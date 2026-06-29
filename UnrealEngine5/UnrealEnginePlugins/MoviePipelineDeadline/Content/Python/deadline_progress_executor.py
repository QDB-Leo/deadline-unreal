# Copyright Epic Games, Inc. All Rights Reserved
"""
Custom local MRQ executor that reports per-frame render progress to Deadline
through the RPC proxy.

UMoviePipelineExecutorBase ticks OnBeginFrame every engine frame. We override it
to read UMoviePipelineExecutorJob::GetStatusProgress() (0..1) on the job being
rendered and push it via proxy.set_progress(0..100).
"""
import unreal

# Deadline RPC proxy for the active render. Set by register_proxy() before the
# render starts. None disables progress reporting (e.g. local test renders).
_active_proxy = None

# Last integer percent pushed, to throttle RPC traffic: we only call
# set_progress when the whole-number percent actually changes.
_last_pushed_pct = -1


def register_proxy(proxy):
    """Register the Deadline RPC proxy and reset the throttle for a new render."""
    global _active_proxy, _last_pushed_pct
    _active_proxy = proxy
    _last_pushed_pct = -1


def _get_active_job():
    """
    Return the job currently rendering.

    Manifests carry a single job, so the first enabled job in the subsystem
    queue is the one rendering. (Multi-job queue assets would need refining,
    but progress would still track the enabled set.)
    """
    subsystem = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    for job in subsystem.get_queue().get_jobs():
        if job.is_enabled():
            return job
    return None


@unreal.uclass()
class DeadlineProgressExecutor(unreal.MoviePipelinePIEExecutor):
    """MoviePipelinePIEExecutor that forwards render progress to Deadline."""

    @unreal.ufunction(override=True)
    def on_begin_frame(self):
        super(DeadlineProgressExecutor, self).on_begin_frame()

        global _last_pushed_pct

        if _active_proxy is None:
            return

        job = _get_active_job()
        if not job:
            return

        try:
            progress = job.get_status_progress()  # 0.0 .. 1.0, updated live
        except Exception as err:
            unreal.log_warning(f"⚠️ Could not read job status progress: {err}")
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
            unreal.log_warning(f"⚠️ Failed to push progress {pct}% to Deadline: {err}")