# Copyright Epic Games, Inc. All Rights Reserved
"""
Custom local MRQ executor that reports per-frame render progress to Deadline
through the RPC proxy.

UMoviePipelineExecutorBase ticks OnBeginFrame every engine frame. We override it
to read UMoviePipelineExecutorJob::GetStatusProgress() (0..1) on the job being
rendered and push it via proxy.set_progress(0..100).

In the editor it renders in PIE (MoviePipelinePIEExecutor). In -game (GameMode
plugin info) the editor modules don't exist: it renders in the game world
(MoviePipelineInProcessExecutor), which opens the job's map itself.
"""
import unreal

from mrq_cli_modes.utils import movie_pipeline_queue

# Deadline RPC proxy for the active render. Set by register_proxy() before the
# render starts. None disables progress reporting (e.g. local test renders).
_active_proxy = None

# Last integer percent pushed, to throttle RPC traffic: we only call
# set_progress when the whole-number percent actually changes.
_last_pushed_pct = -1

# -game: callables for each finished shot, bound on the pipeline the in-process
# executor creates once the job's map is open (see _bind_pipeline). Only the PIE
# executor relays the pipeline's per-shot delegate itself.
_shot_finished_callables = []
_pipeline_bound = False


def register_proxy(proxy):
    """Register the Deadline RPC proxy and reset the throttle for a new render."""
    global _active_proxy, _last_pushed_pct, _shot_finished_callables, _pipeline_bound
    _active_proxy = proxy
    _last_pushed_pct = -1
    _shot_finished_callables = []
    _pipeline_bound = False


def add_shot_finished_callable(executor, callable_):
    """
    Calls callable_(MoviePipelineOutputData) after each shot's files are written,
    in the editor (PIE executor's delegate) as in -game (the pipeline's).
    """
    if hasattr(executor, "on_individual_shot_work_finished_delegate"):
        executor.on_individual_shot_work_finished_delegate.add_callable(callable_)
    else:
        _shot_finished_callables.append(callable_)


def _bind_pipeline(executor):
    """
    -game: binds on the running pipeline, once per render, what the PIE executor
    does itself in the editor:
    - the shot finished callables;
    - an unsuccessful end (canceled, shut down early) reported as an error of the
      executor: the in-process executor ignores it and finishes as if the render
      had succeeded, which would complete the task with frames missing.
    The executor doesn't expose its pipeline: the one not finished yet is this
    render's (a previous task's may not be collected yet in the same session).
    """
    global _pipeline_bound
    if _pipeline_bound:
        return
    for pipeline in unreal.ObjectIterator(unreal.MoviePipelineBase):
        if pipeline.get_name().startswith("Default__"):
            continue
        if pipeline.get_pipeline_state() == unreal.MovieRenderPipelineState.FINISHED:
            continue
        for callable_ in _shot_finished_callables:
            pipeline.on_movie_pipeline_shot_work_finished_delegate.add_callable(callable_)

        def on_work_finished(output_data):
            if not output_data.success:
                executor.on_executor_errored_delegate.broadcast(
                    executor, None, True, "The render ended unsuccessfully (canceled or shut down early)"
                )

        pipeline.on_movie_pipeline_work_finished_delegate.add_callable(on_work_finished)
        _pipeline_bound = True
        unreal.log(f"Render callbacks bound on {pipeline.get_name()}")
        return


def _get_active_job():
    """
    Return the job currently rendering.

    Manifests carry a single job, so the first enabled job in the render queue
    is the one rendering. (Multi-job queue assets would need refining,
    but progress would still track the enabled set.)
    """
    for job in movie_pipeline_queue.get_jobs():
        if job.is_enabled():
            return job
    return None


def _push_progress():
    """Push the active job's progress to Deadline, once per whole percent."""
    global _last_pushed_pct

    if _active_proxy is None:
        return

    job = _get_active_job()
    if not job:
        return

    try:
        progress = job.get_status_progress()  # 0.0 .. 1.0, updated live
    except Exception as err:
        unreal.log_warning(f"Could not read job status progress: {err}")
        return

    pct = max(0, min(100, int(round(progress * 100.0))))

    # Throttle: skip if the whole-number percent hasn't changed, otherwise
    # we'd hit the RPC server on every engine tick.
    if pct == _last_pushed_pct:
        return
    _last_pushed_pct = pct

    try:
        _active_proxy.set_progress(float(pct))
    except Exception as err:
        unreal.log_warning(f"Failed to push progress {pct}% to Deadline: {err}")


if hasattr(unreal, "MoviePipelinePIEExecutor"):

    @unreal.uclass()
    class DeadlineProgressExecutor(unreal.MoviePipelinePIEExecutor):
        """MoviePipelinePIEExecutor that forwards render progress to Deadline."""

        @unreal.ufunction(override=True)
        def on_begin_frame(self):
            super(DeadlineProgressExecutor, self).on_begin_frame()
            _push_progress()

else:

    @unreal.uclass()
    class DeadlineProgressExecutor(unreal.MoviePipelineInProcessExecutor):
        """MoviePipelineInProcessExecutor (-game) that forwards render progress to Deadline."""

        @unreal.ufunction(override=True)
        def on_begin_frame(self):
            super(DeadlineProgressExecutor, self).on_begin_frame()
            _bind_pipeline(self)
            _push_progress()
