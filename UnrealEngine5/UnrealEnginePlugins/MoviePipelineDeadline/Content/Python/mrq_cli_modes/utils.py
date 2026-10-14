# Copyright Epic Games, Inc. All Rights Reserved

import unreal

from collections import namedtuple
from getpass import getuser

# The render queue. In the editor, the MRQ queue subsystem's (the Movie Render Queue
# window's). In -game (GameMode plugin info) the editor modules don't exist: a queue
# of our own, that the executor renders directly (execute_render)
if hasattr(unreal, "MoviePipelineQueueSubsystem"):
    pipeline_subsystem = unreal.get_editor_subsystem(
        unreal.MoviePipelineQueueSubsystem
    )
    movie_pipeline_queue = pipeline_subsystem.get_queue()
else:
    pipeline_subsystem = None
    movie_pipeline_queue = unreal.new_object(unreal.MoviePipelineQueue)

pipeline_executor = None

# Graphs of the jobs being rendered, changed in memory by the overrides (output,
# frame range) and never saved: referenced here until the next render. In -game the
# executor opens the job's map, which collects garbage: an unreferenced graph would
# be reloaded from disk, without the overrides
_graphs_in_render = []


def keep_graphs_loaded(jobs):
    """
    Keep the graphs (and subgraphs) of these jobs loaded until the next call, so
    their in-memory overrides reach the render. See _graphs_in_render.
    """
    global _graphs_in_render
    graphs = []
    for job in jobs:
        graph = job.get_graph_preset()
        if graph:
            graphs += [graph] + list(graph.get_all_contained_subgraphs())
    _graphs_in_render = graphs


def get_project_settings():
    """
    The Movie Render Pipeline project settings. Editor-only: read when needed, not
    at import, so this module loads in -game
    """
    return unreal.get_default_object(unreal.MovieRenderPipelineProjectSettings)


def get_executor_instance(is_remote):
    """
    Method to return an instance of a render executor

    :param bool is_remote: Flag to use the local or remote executor class
    :return: Executor instance
    """
    is_soft_class_object = True
    project_settings = get_project_settings()
    # Convert the SoftClassPath into a SoftClassReference.
    # local executor class from the project settings
    try:
        class_ref = unreal.SystemLibrary.conv_soft_class_path_to_soft_class_ref(
            project_settings.default_local_executor
        )
    # For Backwards compatibility. Older version returned a class object from
    # the project settings
    except TypeError:
        class_ref = project_settings.default_local_executor
        is_soft_class_object = False

    if is_remote:
        try:
            # Get the remote executor class
            class_ref = (
                unreal.SystemLibrary.conv_soft_class_path_to_soft_class_ref(
                    project_settings.default_remote_executor
                )
            )
        except TypeError:
            class_ref = project_settings.default_remote_executor
            is_soft_class_object = False

    if not class_ref:
        raise RuntimeError(
            "Failed to get a class reference to the default executor from the "
            "project settings. Check the logs for more details."
        )

    if is_soft_class_object:
        # Get the executor class as this is required to get an instance of
        # the executor
        executor_class = unreal.SystemLibrary.load_class_asset_blocking(
            class_ref
        )
    else:
        executor_class = class_ref

    global pipeline_executor
    pipeline_executor = unreal.new_object(executor_class)

    return pipeline_executor


def execute_render(is_remote=False, executor_instance=None, is_cmdline=False):
    """
    Starts a render

    :param bool is_remote: Flag to use the local or remote executor class
    :param executor_instance: Executor instance used for rendering
    :param bool is_cmdline: Flag to determine if the render was executed from a commandline.
    """

    if not executor_instance:
        executor_instance = get_executor_instance(is_remote)

    if is_cmdline:
        setup_editor_exit_callback(executor_instance)

    # Start the Render
    unreal.log("MRQ job started...")
    unreal.log(f"Is remote render: {is_remote}")

    if pipeline_subsystem:
        pipeline_subsystem.render_queue_with_executor_instance(executor_instance)
    else:
        # -game: no subsystem, the in-process executor loads the job's map itself
        executor_instance.execute(movie_pipeline_queue)

    return executor_instance


def setup_editor_exit_callback(executor_instance):
    """
    Setup callbacks for when you need to close the editor after a render

    :param executor_instance: Movie Pipeline executor instance
    """

    unreal.log("Executed job from commandline, setting up shutdown callback..")

    # add a callable to the executor to be executed when the pipeline is done rendering
    executor_instance.on_executor_finished_delegate.add_callable(
        shutdown_editor
    )
    # add a callable to the executor to be executed when the pipeline fails to render
    executor_instance.on_executor_errored_delegate.add_callable(
        executor_failed_callback
    )


def shutdown_editor(movie_pipeline=None, results=None):
    """
    This method shutdown the editor
    """
    unreal.log("Rendering is complete! Exiting...")
    unreal.SystemLibrary.quit_editor()


def executor_failed_callback(executor, pipeline, is_fatal, error):
    """
    Callback executed when a job fails in the editor
    """
    unreal.log_error(
        f"An error occurred while executing a render.\n\tError: {error}"
    )

    unreal.SystemLibrary.quit_editor()


def get_asset_data(name_or_path, asset_class):
    """
    Get the asset data for the asset name or path based on its class.

    :param str name_or_path: asset name or package name
    :param str asset_class: Asset class filter to use when looking for assets in registry
    :raises RuntimeError
    :return: Asset package if it exists
    """
    # Get all the specified class assets in the project.
    # This is the only mechanism we can think of at the moment to allow
    # shorter path names in the commandline interface. This will allow users
    # to only provide the asset name or the package path in the commandline
    # interface based on the assumption that all assets are unique
    asset_registry = unreal.AssetRegistryHelpers.get_asset_registry()

    # If the asset registry is still loading, wait for it to finish
    if asset_registry.is_loading_assets():
        unreal.log_warning("Asset Registry is loading, waiting to complete...")
        asset_registry.wait_for_completion()

        unreal.log("Asset Registry load complete!")

    assets = asset_registry.get_assets(
        unreal.ARFilter(class_names=[asset_class])
    )

    # This lookup could potentially be very slow
    for asset in assets:
        # If a package name is provided lookup the package path. If a
        # packages startwith a "/" this signifies a content package. Content
        # packages can either be Game or plugin. Game content paths start
        # with "/Game" and plugin contents startswith /<PluginName>
        if name_or_path.startswith("/"):
            # Reconstruct the package path into a package name. eg.
            # /my/package_name.package_name -> /my/package_name
            name_or_path = name_or_path.split(".")[0]
            if asset.package_name == name_or_path:
                return asset
        else:
            if asset.asset_name == name_or_path:
                return asset
    else:
        raise RuntimeError(f"`{name_or_path}` could not be found!")


def setup_remote_render_jobs(batch_name, job_preset, render_jobs):
    """
    This function sets up a render job with the options for a remote render.
    This is configured currently for deadline jobs.

    :param str batch_name: Remote render batch name
    :param str job_preset: Job Preset to use for job details
    :param list render_jobs: The list of render jobs to apply the ars to
    """

    unreal.log("Setting up Remote render executor.. ")

    # Update the settings on the render job.
    # Currently, this is designed to work with deadline

    # Make sure we have the relevant attribute on the jobs. This remote cli
    # setup can be used with out-of-process rendering and not just deadline.
    unset_job_properties = []
    for job in render_jobs:
        if hasattr(job, "batch_name") and not batch_name:
            unset_job_properties.append(job.name)

        if hasattr(job, "job_preset") and not job_preset:
            unset_job_properties.append(job.name)

    # If we find a deadline property on the job, and it's not set, raise an
    # error
    if unset_job_properties:
        raise RuntimeError(
            "These jobs did not have a batch name, preset name or preset "
            "library set. This is a requirement for deadline remote rendering. "
            "{jobs}".format(
                jobs="\n".join(unset_job_properties))
        )

    for render_job in render_jobs:
        render_job.batch_name = batch_name
        render_job.job_preset = get_asset_data(
            job_preset,
            "DeadlineJobPreset"
        ).get_asset()


def set_job_state(job, enable=False):
    """
    This method sets the state on a current job to enabled or disabled

    :param job: MoviePipeline job to enable/disable
    :param bool enable: Flag to determine if a job should be or not
    """

    if enable:
        # Check for an enable attribute on the job and if not move along.
        # Note: `Enabled` was added to MRQ that allows disabling all shots in
        #  a job. This also enables backwards compatibility.
        try:
            if not job.enabled:
                job.enabled = True
        except AttributeError:
            # Legacy implementations assumes the presence of a job means its
            # enabled
            return

    try:
        if job.enabled:
            job.enabled = False
    except AttributeError:
        # If the attribute is not available, go through and disable all the
        # associated shots. This behaves like a disabled job
        for shot in job.shot_info:
            unreal.log_warning(
                f"Disabling shot `{shot.inner_name}` from current render job `{job.job_name}`"
            )
            shot.enabled = False


def update_render_output(
    job,
    output_dir=None, output_filename=None, override_output=None
):
    """
    Updates that output directory and filename on a render job

    :param job: MRQ job
    :param str output_dir: Output directory for renders
    :param str output_filename: Output filename
    """

    # Get the job output settings
    output_setting = job.get_configuration().find_setting_by_class(
        unreal.MoviePipelineOutputSetting
    )

    if output_dir:
        new_output_dir = unreal.DirectoryPath()
        new_output_dir.set_editor_property(
            "path",
            output_dir
        )
        unreal.log_warning(
            f"Overriding output directory! New output directory is `{output_dir}`."
        )
        output_setting.output_directory = new_output_dir

    if output_filename:
        unreal.log_warning(
            f"Overriding filename format! New format is `{output_filename}`."
        )

        output_setting.file_name_format = output_filename

    if override_output != None and override_output != output_setting.override_existing_output:
        unreal.log_warning(
            f"Overriding output files: `{str(override_output)}`."
        )

        output_setting.override_existing_output = override_output


def apply_graph_output_overrides(job, output_dir=None, output_filename=None, override_output=None):
    """
    update_render_output for a graph job: sets the overrides on the Global Output
    nodes (directory, overwrite existing output) and the file output nodes (filename
    format) of the job's graph and of its subgraphs, so the override wins wherever the
    graph sets them. Changes the loaded graphs only (they are not saved), which is
    what the worker renders.
    """
    graph = job.get_graph_preset()
    graphs = [graph] + list(graph.get_all_contained_subgraphs())

    for g in graphs:
        for node in g.get_nodes_for_branch(unreal.MovieGraphGlobalOutputSettingNode, "Globals", False):
            if output_dir:
                node.set_editor_property("output_directory", unreal.DirectoryPath(output_dir))
                node.set_editor_property("override_output_directory", True)
            if override_output is not None:
                node.set_editor_property("overwrite_existing_output", bool(override_output))
                node.set_editor_property("override_b_overwrite_existing_output", True)
        if output_filename:
            for branch in g.get_branch_names():
                for node in g.get_nodes_for_branch(unreal.MovieGraphFileOutputNode, str(branch), False):
                    node.set_editor_property("file_name_format", output_filename)
                    node.set_editor_property("override_file_name_format", True)

    if output_dir:
        unreal.log_warning(f"Overriding output directory on the graph! New output directory is `{output_dir}`.")
    if output_filename:
        unreal.log_warning(f"Overriding filename format on the graph! New format is `{output_filename}`.")
    if override_output is not None:
        unreal.log(f"Overwrite existing output on the graph: {bool(override_output)}")


def update_queue(
    jobs=None,
    shots=None,
    all_shots=False,
    user=None,
):
    """
    This function configures and renders a job based on the arguments

    :param list jobs: MRQ jobs to render
    :param list shots: Shots to render from jobs
    :param bool all_shots: Flag for rendering all shots
    :param str user: Render user
    """

    # Iterate over all the jobs and make sure the jobs we want to
    # render are enabled.
    # All jobs that are not going to be rendered will be disabled if the
    # job enabled attribute is not set or their shots disabled.
    # The expectation is, If a job name is specified, we want to render the
    # current state of that job.
    # If a shot list is specified, we want to only render that shot alongside
    # any other whole jobs (job states) that are explicitly specified,
    # else other jobs or shots that are not
    # needed are disabled
    for job in movie_pipeline_queue.get_jobs():
        enable_job = False

        # Get a list of jobs to enable.
        # This will enable jobs in their current queue state awaiting other
        # modifications if shots are provided, if only the job name is
        # specified, the job will be rendered in its current state
        if jobs and (job.job_name in jobs):
            enable_job = True

        # If we are told to render all shots. Enable all shots for all jobs
        if all_shots:
            for shot in job.shot_info:
                shot.enabled = True

            # set the user for the current job
            job.author = user or getuser()

            # Set the job to enabled and move on to the next job
            set_job_state(job, enable=True)

            continue

        # If we have a list of shots, go through the shots associated
        # with this job, enable the shots that need to be rendered and
        # disable the others
        if shots and (not enable_job):
            for shot in job.shot_info:
                if shot.inner_name in shots or (shot.outer_name in shots):
                    shot.enabled = True
                    enable_job = True
                else:
                    unreal.log_warning(
                        f"Disabling shot `{shot.inner_name}` from current render job `{job.job_name}`"
                    )
                    shot.enabled = False

        if enable_job:
            # Set the author on the job
            job.author = user or getuser()

        # Set the state of the job by enabling or disabling it.
        set_job_state(job, enable=enable_job)


def find_graph_variable(job, graph, name):
    """
    Where the value of the graph variable `name` comes from, for this job:
    the job's graph if it has the variable, otherwise the first of its subgraphs
    (e.g. a parent graph) that has it. Its value there is the job override if
    checked, otherwise the variable's own value (see get_graph_variable_value).

    :returns: (variable, job overrides container of that graph, graph), or
              (None, None, None) when no graph has the variable
    """
    for candidate in [graph] + list(graph.get_all_contained_subgraphs()):
        variable = candidate.get_variable_by_name(name)
        if variable:
            return variable, job.get_or_create_variable_overrides(candidate), candidate
    return None, None, None


def get_graph_variable_value(job, graph, name):
    """
    Serialized value of the graph variable `name` for this job, by precedence:
    1. the job override, if checked, of the variable in the job's graph;
    2. that variable's own value;
    3. the variable absent from the job's graph: the same in the first subgraph
       that has it.
    An unchecked override's value is never used. None when no graph has it.
    """
    variable, overrides, _ = find_graph_variable(job, graph, name)
    if not variable:
        return None
    if overrides.get_variable_assignment_enable_state(variable):
        return overrides.get_value_serialized_string(variable)
    return variable.get_value_serialized_string()


RenderSetting = namedtuple("RenderSetting", "extra_info node_class prop minimum")

# Render settings of a graph job shown in the Monitor, in the job's Extra Info N, and
# set back on the graph by the worker before each task: change one in the job's
# properties and requeue. Each is a property of a Globals node, keyed by the
# property's name (its pin on the node).
RENDER_SETTINGS = {
    "TemporalSampleCount": RenderSetting(0, "MovieGraphSamplingMethodNode", "temporal_sample_count", 1),
    "NumWarmUpFrames": RenderSetting(1, "MovieGraphWarmUpSettingNode", "num_warm_up_frames", 0),
}


def evaluate_job_graph(job, graph, shot=None, shot_count=1):
    """
    The job's graph flattened for one shot, with the job's variable overrides, as
    MRG evaluates it for the render: (evaluated config or None, error)
    """
    context = unreal.MovieGraphTraversalContext()
    context.job = job
    context.shot = shot
    context.root_graph = graph
    context.shot_count = shot_count
    return graph.create_flattened_graph(context)


def _render_setting_nodes(graph, setting):
    """
    The setting's nodes in the Globals of the graph and of its subgraphs, as
    (graph, node)
    """
    node_class = getattr(unreal, setting.node_class)
    return [
        (g, node)
        for g in [graph] + list(graph.get_all_contained_subgraphs())
        for node in g.get_nodes_for_branch(node_class, "Globals", False)
    ]


def get_render_settings(job, shot=None, shot_count=1):
    """
    The RENDER_SETTINGS a graph job renders a shot with, as {name: value}: from the
    graph evaluated with the job's variable overrides, so whatever sets them (a
    variable wired to the node, a subgraph, the node itself). Only the settings
    whose node is in the graph or its subgraphs: the others can't be changed.
    """
    graph = job.get_graph_preset()
    evaluated, error = evaluate_job_graph(job, graph, shot, shot_count)
    if not evaluated:
        raise RuntimeError(f"Could not evaluate the graph `{graph.get_name()}`: {error}")

    values = {}
    for name, setting in RENDER_SETTINGS.items():
        if _render_setting_nodes(graph, setting):
            node = evaluated.get_setting_for_branch(getattr(unreal, setting.node_class), "Globals")
            values[name] = node.get_editor_property(setting.prop)
    return values


def apply_render_settings(job, requested):
    """
    Sets the render settings asked in Deadline ({name: value}, see RENDER_SETTINGS)
    on a graph job, those that differ from what it renders with: on each of the
    setting's nodes, the job override of the variable wired to the property, else
    the property on the node. Changes the job and the loaded graphs only (not
    saved). Then checks that every enabled shot renders with them.

    :raises RuntimeError: a setting the graph doesn't take (no such node, property
        wired to something else than a variable)
    """
    graph = job.get_graph_preset()
    shots = [shot for shot in job.shot_info if shot.enabled] or [None]

    def mismatches():
        found = {}
        for shot in shots:
            current = get_render_settings(job, shot, len(shots))
            for name, value in requested.items():
                if current.get(name) != value:
                    found[name] = current.get(name)
        return found

    to_change = mismatches()
    if not to_change:
        unreal.log(f"Render settings as in Deadline: {requested}")
        return

    for name, current in to_change.items():
        setting = RENDER_SETTINGS[name]
        value = requested[name]
        unreal.log(f"`{name}` changed in Deadline (Extra Info {setting.extra_info}): {current} -> {value}")
        for owner, node in _render_setting_nodes(graph, setting):
            pin = node.get_input_pin(name)
            source = pin.get_first_connected_pin(True) if pin and pin.is_connected() else None
            if not source:
                node.set_editor_property(setting.prop, value)
                node.set_editor_property(f"override_{setting.prop}", True)
                unreal.log(f"`{name}` = {value} on `{node.get_name()}` of `{owner.get_name()}`")
            elif isinstance(source.node, unreal.MovieGraphVariableNode):
                variable = source.node.get_variable()
                overrides = job.get_or_create_variable_overrides(owner)
                overrides.set_value_serialized_string(variable, str(value))
                overrides.set_variable_assignment_enable_state(variable, True)
                unreal.log(f"`{name}` = {value} through the variable `{variable.get_member_name()}` of `{owner.get_name()}`")
            else:
                unreal.log_warning(
                    f"`{name}` of `{node.get_name()}` in `{owner.get_name()}` comes from "
                    f"`{source.node.get_name()}`, not a variable: not set"
                )

    left = mismatches()
    if left:
        raise RuntimeError(
            "Render settings changed in Deadline that the graph doesn't take: " + ", ".join(
                f"{name} = {requested[name]} (Extra Info {RENDER_SETTINGS[name].extra_info}), "
                + (f"renders with {current}" if current is not None else f"no {RENDER_SETTINGS[name].node_class} in the graph")
                for name, current in left.items()
            )
        )
    unreal.log(f"Render settings from Deadline applied: {requested}")


def apply_frame_range_override(job, start_frame, end_frame, use_output_node=False):
    """
    Override a graph job's playback range via its user-exposed `Start`/`End`
    variables (checked job override), on the graph that has them: the job's
    graph, or else the subgraph found by find_graph_variable. Without those
    variables and with use_output_node, the range goes on the graph's Global
    Output node instead. end_frame is exclusive, as in the graph.
    """
    graph = job.get_graph_preset()
    if not graph:
        unreal.log_warning(
            f"Job `{job.job_name}` has no graph preset - skipping frame range override."
        )
        return False

    start_var, overrides, owner = find_graph_variable(job, graph, "Start")
    end_var, _, end_owner = find_graph_variable(job, graph, "End")
    if not start_var or not end_var or end_owner != owner:
        if use_output_node:
            return _set_output_node_range(graph, start_frame, end_frame)
        unreal.log_warning(
            f"Neither the graph nor its subgraphs expose both 'Start' and 'End' "
            f"variables - cannot apply frame range override."
        )
        return False
    unreal.log(f"Frame range override through the 'Start'/'End' variables of `{owner.get_name()}`")

    new_start = _set_range_variable(overrides, start_var, start_frame)
    new_end = _set_range_variable(overrides, end_var, end_frame)

    unreal.log(
        f"Frame range override on `{job.job_name}`: {start_frame}-{end_frame} "
        f"(Start ={new_start}, End ={new_end})"
    )
    return new_start and new_end


def _set_output_node_range(graph, start_frame, end_frame):
    """
    Set the custom playback range on the graph's Global Output node(s), for
    graphs without `Start`/`End` variables. Changes the loaded graph only (it is
    not saved), which is what the worker renders.
    """
    nodes = graph.get_nodes_for_branch(unreal.MovieGraphGlobalOutputSettingNode, "Globals", False)
    if not nodes:
        unreal.log_warning(
            "Graph has neither 'Start'/'End' variables nor a Global Output node "
            "in Globals - cannot apply frame range override."
        )
        return False

    for node in nodes:
        for prop, frame in (("custom_playback_range_start", start_frame),
                            ("custom_playback_range_end", end_frame)):
            # Type MUST be Custom, as for the variables
            node.set_editor_property(prop, unreal.MovieGraphSequencePlaybackRangeBound(
                type=unreal.MovieGraphSequenceRangeType.CUSTOM, value=int(frame)))
            node.set_editor_property(f"override_{prop}", True)

    unreal.log(f"Frame range override on the Global Output node: {start_frame}-{end_frame} (end exclusive)")
    return True


def get_output_node_range(graph):
    """
    Custom playback range set on the graph's Global Output node, as (start, end)
    with end exclusive, or None when the node doesn't override both bounds.
    """
    for node in graph.get_nodes_for_branch(unreal.MovieGraphGlobalOutputSettingNode, "Globals", False):
        bounds = []
        for prop in ("custom_playback_range_start", "custom_playback_range_end"):
            bound = node.get_editor_property(prop)
            if not node.get_editor_property(f"override_{prop}") or bound.type != unreal.MovieGraphSequenceRangeType.CUSTOM:
                break
            bounds.append(bound.value)
        else:
            return tuple(bounds)
    return None


def _set_range_variable(overrides, variable, frame_value):
    """
    Set a MovieGraphSequencePlaybackRangeBound variable to Custom + value.
    Type MUST be Custom or the Value is ignored (SequenceDefault falls back
    to the sequence's own range). Enable the assignment, else the graph
    default is used.
    """
    name = variable.get_member_name()
    frame_value = int(frame_value)
    new = f"(Type=Custom,Value={frame_value})"

    try:
        overrides.set_value_serialized_string(variable, new)
        overrides.set_variable_assignment_enable_state(variable, True)
    except Exception as err:
        unreal.log_warning(f"Could not set/enable `{name}` = {new}: {err}")
        return False

    # Readback proves Type actually flipped.
    try:
        unreal.log(f"`{name}` readback: {overrides.get_value_serialized_string(variable)}")
    except Exception:
        pass

    return True