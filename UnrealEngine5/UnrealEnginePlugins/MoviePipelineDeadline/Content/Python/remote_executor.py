# Copyright Epic Games, Inc. All Rights Reserved

# Built-In
import getpass
import json
import os
import re
import traceback
from collections import OrderedDict

# External
import unreal

from deadline_service import get_global_deadline_service_instance
from deadline_job import DeadlineJob
from deadline_utils import get_deadline_info_from_preset

import p4_utils

try:
    import kitsu_utils
    _has_kitsu = True
except ImportError:
    _has_kitsu = False


project_root = unreal.Paths.project_dir()

#_______P4_______#

class _UnrealLogger:
    info = staticmethod(unreal.log)
    warning = staticmethod(unreal.log_warning)
    error = staticmethod(unreal.log_error)

p4 = p4_utils.get_p4(project_root, logger=_UnrealLogger)

#______HELPERS______#

def get_mrg_resolution(graph, job=None):
    
    def find_resolution_var(g, job):
        all_vars = g.get_variables()
        for var in all_vars:
            if var.get_value_type() != unreal.MovieGraphValueType.STRUCT:
                continue
            default_serialized = var.get_value_serialized_string()
            if 'Resolution=' not in default_serialized:
                continue

            unreal.log(f"🔍 Found resolution variable '{var.get_member_name()}' default: {default_serialized}")
            serialized = default_serialized

            if job:
                try:
                    overrides = job.get_or_create_variable_overrides(g)
                    is_enabled = overrides.get_variable_assignment_enable_state(var)
                    if is_enabled:
                        override_val = overrides.get_value_serialized_string(var)
                        if override_val:
                            unreal.log(f"🔍 Job override active: {override_val}")
                            serialized = override_val
                except Exception as e:
                    unreal.log_warning(f"⚠️ Could not read override: {e}")

            x_match = re.search(r'X=(\d+)', serialized)
            y_match = re.search(r'Y=(\d+)', serialized)
            if x_match and y_match:
                return unreal.IntPoint(int(x_match.group(1)), int(y_match.group(1)))
        return None

    def find_resolution_node(g):
        try:
            node = g.get_node_for_branch(unreal.MovieGraphGlobalOutputSettingNode,"Globals")
            if node.get_editor_property("override_output_resolution"):
                named_res = node.get_editor_property("output_resolution")
                res = named_res.resolution
                return res
        except Exception as e:
            unreal.log_warning(f"⚠️ Error reading node in '{g.get_name()}': {e}")
        return None

    subgraphs = list(graph.get_all_contained_subgraphs())

    # 1. Variable on main graph
    res = find_resolution_var(graph, job)
    if res:
        unreal.log(f"🎬 Resolution from main graph variable: {res.x}x{res.y}")
        return res

    # 2. Variable on subgraphs
    for sg in subgraphs:
        res = find_resolution_var(sg, job)
        if res:
            unreal.log(f"🎬 Resolution from subgraph variable: {res.x}x{res.y}")
            return res

    # 3. Node value on main graph
    res = find_resolution_node(graph)
    if res:
        unreal.log(f"🎬 Resolution from main graph node: {res.x}x{res.y}")
        return res

    # 4. Node value on subgraphs
    for sg in subgraphs:
        res = find_resolution_node(sg)
        if res:
            unreal.log(f"🎬 Resolution from subgraph node: {res.x}x{res.y}")
            return res

    unreal.log_warning("⚠️ No resolution found in graph variables or nodes, caller should apply default")
    return None


def get_mrg_frame_range(graph, job=None):
    """
    Reads the 'Start'/'End' variables exposed by the graph (taking per-job
    overrides into account). This is the SAME control point that
    apply_frame_range_override rewrites on the worker side, so reading it here
    lets us seed Deadline's Frames field (and the original_frame_range metadata)
    with the range the artist actually set on the job.

    :returns: (start, end) if an explicit Custom range is set, otherwise None
              (the caller then falls back to the sequence's playback range).
    """
    if not graph:
        return None

    variables = {v.get_member_name(): v for v in graph.get_variables()}
    start_var, end_var = variables.get("Start"), variables.get("End")
    if not start_var or not end_var:
        unreal.log_warning(
            "⚠️ Graph does not expose 'Start'/'End' — "
            "falling back to the sequence's playback range."
        )
        return None

    def read_serialized(var):
        # Job override takes priority if enabled, otherwise the variable's default value.
        if job:
            try:
                overrides = job.get_or_create_variable_overrides(graph)
                if overrides.get_variable_assignment_enable_state(var):
                    val = overrides.get_value_serialized_string(var)
                    if val:
                        return val
            except Exception as e:
                unreal.log_warning(
                    f"⚠️ Could not read override for '{var.get_member_name()}': {e}"
                )
        return var.get_value_serialized_string()

    def parse_bound(serialized):
        # A MovieGraphSequencePlaybackRangeBound serializes as
        # (Type=Custom,Value=N) or (Type=SequenceDefault,...). Only Custom carries
        # an explicit frame number; SequenceDefault = "the sequence's range",
        # which we signal with None.
        if not serialized:
            return None
        type_match = re.search(r'Type=(\w+)', serialized)
        if type_match and type_match.group(1) != 'Custom':
            return None
        val_match = re.search(r'Value=(-?\d+)', serialized)
        return int(val_match.group(1)) if val_match else None

    start_val = parse_bound(read_serialized(start_var))
    end_val = parse_bound(read_serialized(end_var))

    if start_val is None or end_val is None:
        unreal.log(
            "ℹ️ 'Start'/'End' not set to Custom (or not set) — "
            "falling back to the sequence's playback range."
        )
        return None

    unreal.log(f"🎬 Frame range from graph Start/End: {start_val}-{end_val}")
    return (start_val, end_val)

def create_shot_list(sequence, shots_to_render, target_size, ignore_chunk_size=False, frame_range_override=None):
    # find the shot track in the sequence and derives packing logic. We don't use it for now so everything is in the first if not. We pass one shot per job.
    shots_track = sequence.find_tracks_by_exact_type(unreal.MovieSceneCinematicShotTrack)

    if not shots_track:
        # Our most common scenario
        if frame_range_override:
            start_frame, end_frame = frame_range_override
        else:
            start_frame = sequence.get_playback_start()
            end_frame = sequence.get_playback_end()
        frame_count = end_frame - start_frame

        shots = {"0": sequence.get_name()}
        frame_list = [f"{start_frame}-{end_frame}"]

        unreal.log(
            "\n  shot : " + sequence.get_name() +
            "\n  frame range " + str(start_frame) + "-" + str(end_frame) +
            "\n  total frame count " + str(frame_count)
        )

        return shots, frame_list, False
            
    elif len(shots_track) > 1:
        # Multiple shot tracks found - ask if user wants to continue with packing
        result = unreal.EditorDialog.show_message(
            "Deadline job submission",
            "Found multiple shot tracks in sequence. This may cause issues with shot packing. Continue anyway?",
            unreal.AppMsgType.YES_NO,
            default_value=unreal.AppReturnType.YES
        )

        if result == unreal.AppReturnType.YES:
            ignore_chunk_size = True
        else:
            raise RuntimeError("Multiple shot tracks found and user chose not to continue.")
    else:
        # Exactly one shot track found - this is the ideal case
        ignore_chunk_size = True
            
        # create shot list of tuples containing shot name and frame count
        shot_weights = []
        if ignore_chunk_size:
            target_size = 1
            shot_weights = [(s, idx, 1) for idx, s in enumerate(shots_to_render)]
        else:
            """
            for section in shots_track[0].get_sections():
                start_frame = unreal.MovieSceneSectionExtensions.get_start_frame(section)
                end_frame = unreal.MovieSceneSectionExtensions.get_end_frame(section)
                frame_count = end_frame - start_frame
                shot_seq = section.get_editor_property('sub_sequence')
                shot_name = shot_seq.get_name()
                # need to find shot name as denominated in the job (which is shotseqname.subseqname)
                shot_name = [s for s in shots_to_render if shot_name in s]
                # skip shot not found in shots_to_render (most probably because it is deactivated)
                if not shot_name:
                    continue
                shot_name = shot_name[0]
                # remove the shot from the list, to avoid using the same one when multiple shots have the same name
                # (sequence name will give exact same name, while in shots_to_render same names are extended with a number in braces)
                shots_to_render.remove(shot_name)
                # add shot info to list
                shot_weights.append((shot_name, start_frame, frame_count))
                """

        # sort by decreasing order
        shot_weights.sort(key=lambda x: x[1], reverse=True)
        min_weight = shot_weights[-1][1]

        # pack shots together based on target frame count
        shots = {}
        added_shots = []
        frame_list = []
        task_index = 0
        last_frame = sequence.get_playback_end()
        for shot, start_frame, weight in shot_weights:
            if shot in added_shots:
                continue

            # add shot to current bin
            current_size = weight
            current_bin = [shot]
            added_shots.append(shot)

            # bin can have more, search for more to add
            # only if it would be possible to add at least the smallest shot, otherwise don't bother
            while(current_size + min_weight <= target_size):
                best_shot = None
                best_weight = 0
                min_diff = 1000000
                for other_shot, _, other_weight in shot_weights:
                    if other_shot in added_shots:
                        continue

                    # check absolute difference between target_size and size with this weight added
                    diff = abs(target_size - (current_size + other_weight))

                    # if difference is smaller than before found, replace best match with this one
                    if diff < min_diff:
                        min_diff = diff
                        best_shot = other_shot
                        best_weight = other_weight

                # add found best shot to current bin
                if best_shot:
                    current_size += best_weight
                    current_bin.append(best_shot)
                    added_shots.append(best_shot)
                # not match found, cannot add more
                else:
                    break

            # bin is full, add the shots to the task list
            shots[str(task_index)] = ",".join(current_bin)

            # compute frame range for the task
            #   tasks of only 1 shot uses the actual shot frame range
            #   tasks of multiple shots uses a fake frame range that starts from the last frame of the sequence
            if len(current_bin) == 1:
                end_frame = start_frame + current_size - 1
            else:
                start_frame = last_frame
                end_frame = start_frame + current_size - 1
                last_frame = end_frame + 1

            frame_list.append(f"{start_frame}-{end_frame}")

            unreal.log(
                "task " + str(task_index) +
                "\n  frame range " + str(start_frame) + "-" + str(end_frame) +
                "\n  total frame count " + str(current_size) +
                "\n  shot count " + str(len(current_bin)) +
                "\n  shots " + str(current_bin)
            )
            task_index += 1

        # sort in reverse order, to prevent Deadline from merging sequencial frame ranges into a single one
        frame_list = sorted(frame_list, key=lambda x: int(x.split("-")[0]), reverse=True)

        return shots, frame_list, not ignore_chunk_size

#______SUBMISSION LOGIC______#

@unreal.uclass()
class MoviePipelineDeadlineRemoteExecutor(unreal.MoviePipelineExecutorBase):
    """
    This class defines the editor implementation for Deadline (what happens when you
    press 'Render (Remote)', which is in charge of taking a movie queue from the UI
    and processing it into something Deadline can handle.
    """

    # The queue we are working on, null if no queue has been provided.
    pipeline_queue = unreal.uproperty(unreal.MoviePipelineQueue)
    job_ids = unreal.uproperty(unreal.Array(str))

    # A MoviePipelineExecutor implementation must override this.
    @unreal.ufunction(override=True)
    def execute(self, pipeline_queue):
        """
        This is called when the user presses Render (Remote) in the UI. We will
        split the queue up into multiple jobs. Each job will be submitted to
        deadline separately, with each shot within the job split into one Deadline
        task per shot.
        """

        unreal.log(f"📦 Asked to execute Queue: {pipeline_queue}")
        unreal.log(f"🔍 Queue has {len(pipeline_queue.get_jobs())} jobs")

        # Don't try to process empty/null Queues, no need to send them to
        # Deadline.
        if not pipeline_queue or (not pipeline_queue.get_jobs()):
            self.on_executor_finished_impl()
            return

        # Verify the P4 ticket
        # actionable if the workstation has no valid ticket (manual login required). -> p4 login
        try:
            p4_utils.verify_p4_ticket(p4)
        except RuntimeError as err:
            unreal.log_error(str(err))
            unreal.EditorDialog.show_message(
                "Perforce", str(err), unreal.AppMsgType.OK
            )
            self.on_executor_finished_impl()
            return

        # The user must save their work and check it in so that Deadline
        # can sync it.
        dirty_packages = []
        dirty_packages.extend(
            unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()
        )
        dirty_packages.extend(
            unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()
        )

        # Sometimes the dialog will return `False`
        # even when there are no packages to save. so we are
        # being explict about the packages we need to save
        if dirty_packages:
            if not unreal.EditorLoadingAndSavingUtils.save_dirty_packages_with_dialog(
                True, True
            ):
                message = (
                    "One or more jobs in the queue have an unsaved map/content. "
                    "{packages} "
                    "Please save and check-in all work before submission.".format(
                        packages="\n".join(dirty_packages)
                    )
                )

                unreal.log_error(message)
                unreal.EditorDialog.show_message(
                    "Unsaved Maps/Content", message, unreal.AppMsgType.OK
                )
                self.on_executor_finished_impl()
                return

        # Make sure all the maps in the queue exist on disk somewhere,
        # unsaved maps can't be loaded on the remote machine, and it's common
        # to have the wrong map name if you submit without loading the map.
        has_valid_map = (
            unreal.MoviePipelineEditorLibrary.is_map_valid_for_remote_render(
                pipeline_queue.get_jobs()
            )
        )
        if not has_valid_map:
            message = (
                "One or more jobs in the queue have an unsaved map as "
                "their target map. "
                "These unsaved maps cannot be loaded by an external process, "
                "and the render has been aborted."
            )
            unreal.log_error(message)
            unreal.EditorDialog.show_message(
                "Unsaved Maps", message, unreal.AppMsgType.OK
            )
            self.on_executor_finished_impl()
            return

        self.pipeline_queue = pipeline_queue

        deadline_settings = unreal.get_default_object(
            unreal.MoviePipelineDeadlineSettings
        )

        # Arguments to pass to the executable. This can be modified by settings
        # in the event a setting needs to be applied early.
        # In the format of -foo -bar
        # commandLineArgs = ""
        command_args = []

        # Append all of our inherited command line arguments from the editor.
        in_process_executor_settings = unreal.get_default_object(
            unreal.MoviePipelineInProcessExecutorSettings
        )
        inherited_cmds = in_process_executor_settings.inherited_command_line_arguments

        # Sanitize the commandline by removing any execcmds that may
        # have passed through the commandline.
        # We remove the execcmds because, in some cases, users may execute a
        # script that is local to their editor build for some automated
        # workflow but this is not ideal on the farm. We will expect all
        # custom startup commands for rendering to go through the `Start
        # Command` in the MRQ settings.
        inherited_cmds = re.sub(
            ".*(?P<cmds>-execcmds=[\s\S]+[\'\"])",
            "",
            inherited_cmds
        )

        command_args.extend(inherited_cmds.split(" "))
        command_args.extend(
            in_process_executor_settings.additional_command_line_arguments.split(
                " "
            )
        )

        # Get the project level preset
        project_preset = deadline_settings.default_job_preset

        # Get the job and plugin info string.
        # Note:
        #   Sometimes a project level default may not be set,
        #   so if this returns an empty dictionary, that is okay
        #   as we primarily care about the job level preset.
        #   Catch any exceptions here and continue
        try:
            project_job_info, project_plugin_info = get_deadline_info_from_preset(job_preset=project_preset)

        except Exception:
            pass

        deadline_service = get_global_deadline_service_instance()

        #______Pack Jobs to Tasks______#
        PackJobsToTasks = False

        for key, value in project_job_info.items():
            if key.startswith("ExtraInfoKeyValue"):
                sub_key, sub_val = value.split('=', 1)
                if sub_key == "PackJobsToTasks":
                    PackJobsToTasks = sub_val.lower() in ['true', '1', 't', 'y', 'yes', 'yeah', 'yup', 'certainly', 'uh-huh']
                    
        # If PackShotsToTasks is True, we will try to pack multiple shots into a single task
        if PackJobsToTasks:
            # Gather all enabled jobs
            jobs_to_submit = [job for job in self.pipeline_queue.get_jobs() if job.is_enabled() and job.job_preset]
            if not jobs_to_submit:
                unreal.log_warning("No jobs to submit.")
                return

            # Create one Deadline job with multiple tasks
            job_info, plugin_info = get_deadline_info_from_preset(job_preset=project_preset)
            frames = []
            for idx, job in enumerate(jobs_to_submit):
                # Each Unreal job becomes a task (frame) in Deadline
                frames.append(str(idx))
                # Optionally, store job info per task in ExtraInfoKeyValue or TaskExtraInfoNames
                job_info[f"TaskExtraInfoNames{idx}"] = job.job_name

            job_info["Frames"] = ",".join(frames)
            # Submit one Deadline job
            deadline_job = DeadlineJob(job_info, plugin_info)
            deadline_service = get_global_deadline_service_instance()
            job_id = deadline_service.submit_job(deadline_job)
            unreal.log(f"Submitted one Deadline job with {len(jobs_to_submit)} tasks. JobId: {job_id}")


        #______Submit Each Job Individually______#

        else:
            # Iterate over each job in the queue and submit it to Deadline.
            for job in self.pipeline_queue.get_jobs():

                # Don't send disabled jobs on Deadline
                if not job.is_enabled():
                    unreal.log(f"Ignoring disabled Job `{job.job_name}`")
                    continue

                # Don't process jobs without job preset assigned, it crashes the editor
                if not job.job_preset:
                    unreal.log(f"Ignoring Job without DeadlineJobPreset assigned `{job.job_name}`")
                    continue

                unreal.log(f"Submitting Job `{job.job_name}` to Deadline...")

                # retrieve job's user_data
                user_data = {}
                try:
                    user_data = json.loads(job.user_data)
                except json.decoder.JSONDecodeError as err:
                    # not a dict or empty, make it a dict
                    user_data = {"previous_user_data": job.user_data}

                try:
                    # Create a Deadline job object with the default project level
                    # job info and plugin info
                    deadline_job = DeadlineJob(project_job_info, project_plugin_info)

                    deadline_job_id = self.submit_job(
                        job, dict(user_data), deadline_job, command_args, deadline_service
                    )

                except Exception as err:
                    unreal.log_error(
                        f"Failed to submit job `{job.job_name}` to Deadline, aborting render. \n\tError: {str(err)}"
                    )
                    unreal.log_error(traceback.format_exc())
                    self.on_executor_errored_impl(None, True, str(err))
                    unreal.EditorDialog.show_message(
                        "Submission Result",
                        f"Failed to submit job `{job.job_name}` to Deadline with error:\n{str(err)}. "
                        f"See log for more details.",
                        unreal.AppMsgType.OK,
                    )
                    self.on_executor_finished_impl()
                    return

                if not deadline_job_id:
                    message = (
                        f"A problem occurred submitting `{job.job_name}`. "
                        f"Either the job doesn't have any data to submit, "
                        f"or an error occurred getting the Deadline JobID. "
                        f"This job status would not be reflected in the UI. "
                        f"Check the logs for more details."
                    )
                    unreal.log_warning(message)
                    unreal.EditorDialog.show_message(
                        "Submission Result", message, unreal.AppMsgType.OK
                    )
                    return

                else:
                    unreal.log(f"Deadline JobId: {deadline_job_id}")
                    self.job_ids.append(deadline_job_id)

                    # Store the Deadline JobId in our job (the one that exists in
                    # the queue, not the duplicate) so we can match up Movie
                    # Pipeline jobs with status updates from Deadline.
                    user_data.setdefault("job_ids", []).append(deadline_job_id)
                    job.user_data = json.dumps(user_data)

        #______Submission Result______#

        message = ""
        if not len(self.job_ids):
            message = "No jobs were sent to Deadline, check if enabled."
        else:
            message = (
                f"✅ Successfully submitted {len(self.job_ids)} jobs to Deadline."
                # f"\n\n" +
                # "\n".join(job.job_name for job in self.job_ids) +
                f"\n\nPlease use Deadline Monitor to track render job statuses"
            )

        unreal.log(message)

        unreal.EditorDialog.show_message(
            "Submission Result", message, unreal.AppMsgType.OK
        )

        # Set the executor to finished
        self.on_executor_finished_impl()

    @unreal.ufunction(override=True)
    def is_rendering(self):
        # Because we forward unfinished jobs onto another service when the
        # button is pressed, they can always submit what is in the queue and
        # there's no need to block the queue.
        # A MoviePipelineExecutor implementation must override this. If you
        # override a ufunction from a base class you don't specify the return
        # type or parameter types.
        return False

    def submit_job(self, job, user_data, deadline_job, command_args, deadline_service):
        """
        Submit a new Job to Deadline
        :param job: Queued job to submit
        :param deadline_job: Deadline job object
        :param list[str] command_args: Commandline arguments to configure for the Deadline Job
        :param deadline_service: An instance of the deadline service object
        :returns: Deadline Job ID
        :rtype: str
        """

        # Get the Job Info and plugin Info
        # If we have a preset set on the job, get the deadline submission details
        try:
            job_info, plugin_info = get_deadline_info_from_preset(job_preset_struct=job.get_deadline_job_preset_struct_with_overrides())
            # unreal.log(f"📝 JobInfo : {job_info}")
        # Fail the submission if any errors occur
        except Exception as err:
            raise RuntimeError(
                f"An error occurred getting the deadline job and plugin "
                f"details. \n\tError: {err} "
            )

        # check for required fields in pluginInfo
        if "Executable" not in plugin_info:
            raise RuntimeError("An error occurred formatting the Plugin Info string. \n\tMissing \"Executable\" key")
        elif not plugin_info["Executable"]:
            raise RuntimeError(f"An error occurred formatting the Plugin Info string. \n\tExecutable value cannot be empty")
        if "ProjectFile" not in plugin_info:
            raise RuntimeError("An error occurred formatting the Plugin Info string. \n\tMissing \"ProjectFile\" key")
        elif not plugin_info["ProjectFile"]:
            raise RuntimeError(f"An error occurred formatting the Plugin Info string. \n\tProjectFile value cannot be empty")

        auxilliary_files = []

        # get PreJobScript and check that it is an existing full path
        pre_job_script = job_info.get('PreJobScript')
        if pre_job_script and not os.path.exists(pre_job_script):
            raise RuntimeError(f"PreJobScript path provided is not a valid path: {pre_job_script}")

        # default PreJobScript is PreJob.py file that is included in this plugin
        if not pre_job_script:
            job_info['PreJobScript'] = 'PreJob.py'
            auxilliary_files.append(os.path.join(os.path.dirname(__file__), "PreJob.py"))

        # check for Perforce required field in jobInfo
        environment_key_values = {}
        environment_key_indices = {}
        current_env_index = 0


        for key, value in job_info.items():
            if key.startswith('EnvironmentKeyValue'):
                sub_key, sub_val = value.split('=', 1)
                environment_key_values[sub_key] = sub_val
                environment_key_indices[sub_key] = int(key.replace('EnvironmentKeyValue', ''))
                current_env_index += 1

        # If P4 is enabled, get the latest submitted CL and add it to the job info
        p4_cl = p4_utils.get_latest_submitted_cl(p4, logger=_UnrealLogger)
        if not p4_cl:
            unreal.log_warning("⚠️ Latest submitted P4 CL not found; job will have no version info.")
            p4_cl = -1
        job_info["ExtraInfo9"] = f"submitted_cl={p4_cl}"

        # Not Used
        # p4_stream = environment_key_values.get('P4_stream', 'main') 
        p4_workspace_prefix = environment_key_values.get('P4_workspace_prefix')

        # if workspace prefix is not provided, use project name
        if not p4_workspace_prefix:
            project_path = unreal.Paths.get_project_file_path()
            p4_workspace_prefix, _ = os.path.splitext(os.path.basename(project_path))

            # add P4_workspace_prefix in job info
            job_info[f"EnvironmentKeyValue{current_env_index}"] = f"P4_workspace_prefix={p4_workspace_prefix}"
            current_env_index += 1


        # Update the job info with overrides from the UI
        if job.batch_name:
            job_info["BatchName"] = job.batch_name

        if hasattr(job, "comment") and not job_info.get("Comment"):
            job_info["Comment"] = job.comment

        if not job_info.get("Name") or job_info["Name"] == "Untitled":
            job_info["Name"] = job.job_name

        # Make sure a username is set
        # Priority to job.author, then job_info, finally session user
        username = job.author or job_info.get("UserName") or getpass.getuser()
        job.author = username
        job_info["UserName"] = username

        if unreal.Paths.is_project_file_path_set():
            # Trim down to just "Game.uproject" instead of absolute path.
            game_name_or_project_file = (
                unreal.Paths.convert_relative_path_to_full(
                    unreal.Paths.get_project_file_path()
                )
            )

        else:
            raise RuntimeError(
                "Failed to get a project name. Please set a project!"
            )

        # Create a new queue with only this job in it and save it to disk,
        # then load it, so we can send it with the REST API
        new_queue = unreal.MoviePipelineQueue()
        new_job = new_queue.duplicate_job(job)

        duplicated_queue, manifest_path = unreal.MoviePipelineEditorLibrary.save_queue_to_manifest_file(
            new_queue
        )

        # Convert the queue to text (load the serialized json from disk) so we
        # can send it via deadline, and deadline will write the queue to the
        # local machines on job startup.
        serialized_pipeline = unreal.MoviePipelineEditorLibrary.convert_manifest_file_to_string(
            manifest_path
        )

        # Loop through our settings in the job and let them modify the command
        # line arguments/params.
        new_job.get_configuration().initialize_transient_settings()
        # Look for our Game Override setting to pull the game mode to start
        # with. We start with this game mode even on a blank map to override
        # the project default from kicking in.
        game_override_class = None

        out_url_params = []
        out_command_line_args = []
        out_device_profile_cvars = []
        out_exec_cmds = []
        for setting in new_job.get_configuration().get_all_settings():

            out_url_params, out_command_line_args, out_device_profile_cvars, out_exec_cmds = setting.build_new_process_command_line_args(
                out_url_params,
                out_command_line_args,
                out_device_profile_cvars,
                out_exec_cmds,
            )

            # Set the game override
            # if setting.get_class() == unreal.MoviePipelineGameOverrideSetting.static_class():
            #     game_override_class = setting.game_mode_override

        game_override_class = unreal.load_class(None, "/Script/MovieRenderPipelineCore.MoviePipelineGameMode")
        if not game_override_class:
            raise RuntimeError("Failed to load MoviePipelineGameMode class")

        # custom prescript to execute stuff right when Unreal loads
        out_exec_cmds.append("py custom_unreal_prescript.py")

        # This triggers the editor to start looking for render jobs when it
        # finishes loading.
        out_exec_cmds.append("py mrq_rpc.py")

        # Convert the arrays of command line args, device profile cvars,
        # and exec cmds into actual commands for our command line.
        command_args.extend(out_command_line_args)

        if out_device_profile_cvars:
            # -dpcvars="arg0,arg1,..."
            command_args.append(
                '-dpcvars="{dpcvars}"'.format(
                    dpcvars=",".join(out_device_profile_cvars)
                )
            )

        if out_exec_cmds:
            # -execcmds="cmd0,cmd1,..."
            command_args.append(
                '-execcmds="{cmds}"'.format(cmds=",".join(out_exec_cmds))
            )

        # Add support for telling the remote process to wait for the
        # asset registry to complete synchronously
        command_args.append("-waitonassetregistry")

        unreal.log(f"{command_args}")

        # Build a shot-mask from this sequence, to split into the appropriate
        # number of tasks. Remove any already-disabled shots before we
        # generate a list, otherwise we make unneeded tasks which get sent to
        # machines
        shots_to_render = []
        shots_inner_name = []
        for shot_index, shot in enumerate(new_job.shot_info):
            if not shot.enabled:
                unreal.log(
                    f"Skipped submitting shot {shot_index} in {job.job_name} "
                    f"to server due to being already disabled!"
                )
            else:
                # check inner names, as those are not made unique by Unreal, while same outer names are appended with a number in braces..
                if shot.inner_name in shots_inner_name:
                    result = unreal.EditorDialog.show_message(
                        "Deadline job submission",
                        f"Found twice the same shot name ({shot.inner_name, shot.outer_name}), it may cause issues when writing files if using shot_name or frame_number_shot in folder and frame name."
                        "\nWould you like to submit anyway ?    Else, consider renaming the shots in the sequence (not the shot assets)",
                        unreal.AppMsgType.YES_NO,
                        default_value=unreal.AppReturnType.NO
                    )

                    # return if user chose to not send the job like this
                    if result != unreal.AppReturnType.YES:
                        unreal.log_error(f"Found twice the same shot name ({shot.inner_name, shot.outer_name})")
                        return

                shots_to_render.append(shot.outer_name)
                shots_inner_name.append(shot.inner_name)

        # If there are no shots enabled,
        # "these are not the droids we are looking for", move along ;)
        # We will catch this later and deal with it
        if not shots_to_render:
            unreal.log_warning("No shots enabled in shot mask, not submitting.")
            return

        # Divide the job to render by the chunk size
        # i.e {"O": "my_new_shot"} or {"0", "shot_1,shot_2,shot_4"}
        '''
        chunk_size = int(job_info.get("ChunkSize", 1))
        shots = {}
        frame_list = []
        for index in range(0, len(shots_to_render), chunk_size):

            shots[str(index)] = ",".join(shots_to_render[index : index + chunk_size])

            frame_list.append(str(index))

        job_info["Frames"] = ",".join(frame_list)
        '''

        # Divide the job to render by the chunk size
        # ChunkSize is counted in frames, and the goal is to create tasks of 1 or more shots
        # totalling approximatively ChunkSize frames
        # NOTE: could automagically determine ChunkSize based on the different shots length ?
        target_size = int(job_info.get("ChunkSize", 100))

        # force ChunkSize to a large number, to prevent Deadline from splitting the tasks more than what we give
        job_info["ChunkSize"] = "1000000"

        # get sequence and create frame list for deadline - trying to get the frame range from the graph overrides
        sequence = unreal.SystemLibrary.conv_soft_obj_path_to_soft_obj_ref(new_job.sequence)
        frame_range_override=None
        graph_preset= new_job.get_graph_preset()
        if graph_preset:
            frame_range_override = get_mrg_frame_range(graph_preset, job=new_job)
        shots, frame_list, has_frame_range = create_shot_list(sequence, shots_to_render, target_size, frame_range_override=frame_range_override)

        job_info["Frames"] = ",".join(frame_list)
        unreal.log(f'frame list: {job_info["Frames"]}')

        # enable frame timeout, so that task timeout changes based on actual frame count
        # TODO: add on Job Preset object
        if has_frame_range:
            job_info["EnableFrameTimeouts"] = "1"
        # not using frame ranges, so increase task timeout
        else:
            job_info["TaskTimeoutSeconds"] = "0"
            job_info["EnableFrameTimeouts"] = "0"

        # Get the current index of the ExtraInfoKeyValue pair, we will
        # increment the index, so we do not stomp other settings
        extra_info_key_indexs = set()
        for key in job_info.keys():
            if key.startswith("ExtraInfoKeyValue"):
                _, index = key.split("ExtraInfoKeyValue")
                extra_info_key_indexs.add(int(index))

        # Get the highest number in the index list and increment the number
        # by one
        current_index = max(extra_info_key_indexs) + 1 if extra_info_key_indexs else 0

        # Put the serialized Queue into the Job data but hidden from
        # Deadline UI
        job_info[f"ExtraInfoKeyValue{current_index}"] = f"serialized_pipeline={serialized_pipeline}"

        # Increment the index
        current_index += 1

        # Put the shot info in the job extra info keys
        job_info[f"ExtraInfoKeyValue{current_index}"] = f"shot_info={json.dumps(shots)}"
        current_index += 1

        job_info[f"ExtraInfoKeyValue{current_index}"] = f"original_frame_range={job_info['Frames']}"
        current_index += 1

        # --- Temporal Sample Count (Movie Render Graph variable) ---
        # Reads "TemporalSampleCount" int variable from the graph preset if it exists,
        # and exposes it in ExtraInfo0 for visibility in Deadline Monitor.
        temporal_sample_count = None
        graph_config = job.get_graph_preset()
        if graph_config and _has_kitsu:
            temporal_sample_var, temporal_sample_overrides = kitsu_utils.get_variable_from_graph(
                new_job, graph_config, "TemporalSampleCount"
            )
            if temporal_sample_var and temporal_sample_overrides:
                serialized = temporal_sample_overrides.get_value_serialized_string(temporal_sample_var)
                if not serialized:
                    # fallback: read the default value of the variable itself
                    serialized = temporal_sample_var.get_value_serialized_string()
                temporal_sample_count = int(serialized) if serialized else None
                unreal.log(f"🎞 TemporalSampleCount from graph: {temporal_sample_count}")
            else:
                unreal.log_warning("⚠️ Variable 'TemporalSampleCount' not found in graph preset")
        else:
            unreal.log_warning("⚠️ No graph preset on job, skipping TemporalSampleCount lookup")

        if temporal_sample_count is not None:
            job_info["ExtraInfo0"] = str(temporal_sample_count)


        # Tell Deadline job about output directory and filename
        if job.get_graph_preset() :
            # MRG path
            graph = job.get_graph_preset()
            output_dir = graph.get_output_directory()

            # Resolve MRG tokens to actual values before sending to Deadline
            if '{' in output_dir:
                project_dir = unreal.Paths.convert_relative_path_to_full(
                    unreal.Paths.project_dir()
                ).rstrip('/').rstrip('\\')
                
                output_dir = output_dir.replace('{project_dir}', project_dir)
                output_dir = output_dir.replace('{sequence_name}', new_job.job_name)
                # add other tokens here as needed
                unreal.log(f"🎬 Resolved output dir: {output_dir}")

            # Warn if tokens remain unresolved
            if '{' in output_dir:
                unreal.log_warning(f"⚠️ Output dir still contains unresolved tokens: {output_dir}")


            output_file = ""
            output_resolution = get_mrg_resolution(graph, job=new_job)
            if not output_resolution:
                unreal.log_warning("⚠️ Render resolution not found in graph, defaulting to 1920x1080")
                output_resolution = unreal.IntPoint(1920, 1080)

            unreal.log(f"🎬 Output settings from Graph: {output_dir}, {output_file}")
        else:
            # Classic MoviePipeline path
            output_setting = new_job.get_configuration().find_setting_by_class( unreal.MoviePipelineOutputSetting )
            if output_setting:
                output_dir = output_setting.output_directory.path
                output_file = output_setting.file_name_format
                output_resolution = output_setting.output_resolution
                unreal.log(f"🎬 Output settings from MoviePipeline: {output_dir}")

        # Set the job output directory override on the deadline job
        if new_job.output_directory_override.path:
            job_info[f"ExtraInfoKeyValue{current_index}"] = f"output_directory_override={new_job.output_directory_override.path}"
            current_index += 1
        else:
            job_info[f"ExtraInfoKeyValue{current_index}"] = f"output_directory_override={output_dir}"
            current_index += 1
            # unreal.log_warning(f"No output directory override set on job, using the one from the output settings : {output_dir}.")


        # Set the job filename format override on the deadline job
        if new_job.filename_format_override:
            job_info[f"ExtraInfoKeyValue{current_index}"] = f"filename_format_override={new_job.filename_format_override}"
            current_index += 1



        # TODO: Resolve path formatting based on render settings to make it understandable by Deadline
        job_info["OutputDirectory0"] = output_dir
        # unreal.log(f'✈ output directory: {job_info["OutputDirectory0"]}')

        # TODO: Resolve filename format based on render settings to make it understandable by Deadline
        job_info["OutputFilename0"] = new_job.filename_format_override or output_file

        # add map path to job environment, to be used to preload it
        map_path = unreal.SystemLibrary.conv_soft_obj_path_to_soft_obj_ref(new_job.map).get_path_name()
        job_info[f"EnvironmentKeyValue{current_env_index}"] = f"UEMAP_PATH={map_path}"
        current_env_index += 1

        # Force override of output files, to avoid multiple files when task fails and restart
        job_info[f"EnvironmentKeyValue{current_env_index}"] = f"override_output=1"
        current_env_index += 1

        # finalize user_data to pass it to the Deadline job
        job_info[f"EnvironmentKeyValue{current_env_index}"] = f"P4_PORT={p4.port}"
        current_env_index += 1
        job_info[f"EnvironmentKeyValue{current_env_index}"] = f"P4_USER={p4.user}"
        current_env_index += 1
        #Override here to speify the CL to sync to, instead of head (if sync_to_specific_cl is set)
        job_info[f"EnvironmentKeyValue{current_env_index}"] = f"P4_CL={p4_cl}"
        current_env_index += 1

        if not new_job.filename_format_override:
            unreal.log_warning("⚠️ No filename format override set on job — Deadline Monitor output filename will be empty.")



        # force field "remote" to True, since we're going to send it on farm
        user_data["remote"] = True


        # remove job_ids entry, we don't need to send this to the farm
        # it contains previous Deadline job ids sent from this MRQ job
        user_data.pop("job_ids", None)

        job_info[f"EnvironmentKeyValue{current_env_index}"] = f"MRQ_user_data={json.dumps(user_data)}"
        current_env_index += 1

        command_args.extend(["-nohmd", "-windowed"])

        # Add resolution to commandline arguments
        # to force considering render settings' output resolution instead of limiting to screen resolution
        command_args.append(f"-ResX={output_resolution.x}")
        command_args.append(f"-ResY={output_resolution.y}")
        command_args.append(f"-ForceRes")

        # Build the command line arguments the remote machine will use.
        # The Deadline plugin will provide the executable since it is local to
        # the machine. It will also write out queue manifest to the correct
        # location relative to the Saved folder

        # Get the current commandline args from the plugin info
        plugin_info_cmd_args = [plugin_info.get("CommandLineArguments", "")]

        if not plugin_info.get("ProjectFile"):
            project_file = plugin_info.get("ProjectFile", game_name_or_project_file)
            plugin_info["ProjectFile"] = project_file

        # This is the map included in the plugin to boot up to.
        project_cmd_args = [
            f"MoviePipelineEntryMap?game={game_override_class.get_path_name()}"
        ]

        # Combine all the compiled arguments
        full_cmd_args = project_cmd_args + command_args + plugin_info_cmd_args

        # Remove any duplicates in the commandline args and convert to a string
        full_cmd_args = " ".join(list(OrderedDict.fromkeys(full_cmd_args))).strip()

        # unreal.log(f"Deadline job command line args: {full_cmd_args}")

        # Update the plugin info with the commandline arguments
        plugin_info.update(
            {
                "CommandLineArguments": full_cmd_args,
                "CommandLineMode": plugin_info.get("CommandLineMode", "false"),
            }
        )

        # add auxilliary files to the job, if any
        if auxilliary_files:
            job_info['AuxFiles'] = auxilliary_files

        deadline_job.job_info = job_info
        deadline_job.plugin_info = plugin_info

        # Submit the deadline job
        return deadline_service.submit_job(deadline_job)

