# deadline-unreal
Deadline and Unreal plugins to send Unreal render jobs to Deadline.

Improved version of the DwarfLabs code.
This is still a WIP but helps me a lot. I'm using it with UE 5.6 and 5.7

Major changes from DwarfLabs version :
- Supports MRG, retrieves correct resolution from either job override, node value or subgraph
  Graph variables the plugin reads or sets (`Start`/`End`, `TemporalSampleCount`) follow the render's precedence: in the job's graph, the job override if checked, else the variable's value; absent from the job's graph, the same in the first subgraph that has it (e.g. a parent graph). An unchecked override is never used. Everything the render needs travels in the job's serialized manifest; what the plugin computes (Deadline frames, `-ResX/-ResY`, the temporal samples shown in the Monitor) mirrors it.
- Render progress tracking inside Deadline Task
- Changed handling of P4 connection (ticket-based), syncs to head by default
- FrameRange override support, so you can change framerange inside Deadline and requeue. Used in conjonction with a perforce change pushed to depot so you don't have to resubmit a job.
  The job's frames are the sequence's frames, last one included (a 10-frame shot starting at 0 is `0-9`). The worker applies them through the `Start`/`End` variables (job's graph, or else a subgraph), or the Global Output node's custom playback range when the graph has no such variables. Sequences with a shot track get one task per shot, whose frames only number the shots: no override for those.
- GPU crash detection (editor mode): on a D3D/DXGI crash line in Unreal's log, the task fails and Deadline requeues it, and the next attempt resumes from the last frame the task wrote (rendered again, its write may be cut). The job shows it as `lastframerendered`. Sequences with a Play Rate or Time Warp track (`frames_remapped=1`) and shot tasks don't resume: their task is failed for good.
- A render that doesn't succeed fails its task: a canceled or errored render, an error reported by Unreal, Unreal exiting mid task. Tasks have a timeout per frame: the job preset's Task Timeout Seconds, 300 s when it is 0 (enough for a 4K path traced frame), times the task's frames.
- Output overrides of the job (directory, filename format, overwrite existing output) apply to graph jobs too: the worker sets them on the graph's Global Output and file output nodes.
- Before submitting, the project's files opened in Perforce but not submitted are listed: the farm renders the depot, without them. The job notes the project's latest submitted CL (`submitted_cl`) and the CL the worker actually rendered (`synced_cl`).

Todo :
- GPU crash resume for sequences with a Play Rate / Time Warp track: map the output frames back to the sequence's


Binaries for Unreal Plugins are provided as they can't be automatically generated as usual

Feel free to fork and improve it as you need :)


# Setup

## Unreal

Unreal side, there are 2 plugins (**MoviePipelineDeadline** and **UnrealDeadlineService**, located in `UnrealEnginePlugins`) that you need to add in your projet and compile.

In the project settings, you need to modify the **Default Remote Executor** and **Default Executor job** (in `Plugins -> Movie render pipeline`) to use the Deadline version of them: **MoviePipelineDeadlineRemoteExecutor** and **MoviePipelineDeadlineExecutorJob**.
You can also define a default **Deadline job preset** in the project settings, in `Plugins -> Movie Pipeline Deadline`.
This asset defines default values for most of the Deadline job options, and allows to choose what options can or cannot be overriden by the user from the MRQ (though we had issues making Unreal correctly do that and ended up modifying the default shown properties in the c++ directly..).
There is an example of such asset in the repository: **DJP_DeadlineJobPresetExample_EditorMode.uasset**.

Each task runs in Unreal's editor (exactly like with the UI): the Deadline plugin starts an RPC server, Unreal a client, and Unreal asks Deadline what it should render. Unreal stays open between tasks of the same job, and Python and the editor blueprint nodes work (the Kitsu callbacks of the graphs need them). You can add the `-renderoffscreen` argument (in **CommandLineArguments**) to allow running this job on a farm as a service with no UI.

There is a dependancy on Perforce's python API. When sending a job to Deadline, the user needs to specify the Perforce's changelist id (CL) that the worker should sync to in order to do the render with the correct version of the project. This CL is then checked against Perforce to ensure its validity, and propose the user to use the last valid one if the provided CL is not valid.

We also implemented a feature of **shot packing**, that will try to group small shots (few frames) together in a same task. The goal is to capitalize on Unreal opening time by rendering multiple shots with the same Unreal instance. It also helps optimizing render time by reducing render time differences between tasks.

The most important file if you need to make modifications is `remote_executor.py`, which is what is called when you press "Render remote" in Unreal and will create and send the job to Deadline.
`custom_unreal_prescript.py` is also useful to modify.


## Deadline

Deadline side, there is also the dependency to Perforce's python API. We added it to pythonsync3.zip archive so that it is deployed on all farmers, but you could just make sure it's deployed somewhere reachable by the farmers and add it to the **PYTHONPATH**.

Relative to Perforce, the `JobPreLoad.py` will try to sync the farmer's Perforce repository based on the job's CL.
To retrieve the local Perforce workspace, we use a pattern to match existing workspaces. You probably will need to adapt this part to your own pattern.

## Job data

Each value the job carries has one place, set by what reads it:

- **EnvironmentKeyValue**: only what the Unreal process must read itself, before it reaches Deadline through the RPC, as Deadline makes them its environment variables. `UEMAP_PATH` (the map the pre-script loads).
- **PluginInfo**: how to run Unreal, editable in the Monitor's job properties (UnrealEngine5 settings). `Executable`, `ProjectFile`, `CommandLineArguments`, `OverrideExistingOutput`.
- **ExtraInfoKeyValue**: everything else, read by `JobPreLoad.py` and the Deadline plugin (or by Unreal through the RPC), and what they write during the render.
  - Set in the job preset: `P4_workspace_prefix` (else the project name), `SyncToSpecificCL`, `SyncSubPath`.
  - Set at submission: `P4_PORT`, `P4_USER`, `P4_CL`, `serialized_pipeline`, `shot_info`, `original_frame_range`, `frame_range_mode`, `frames_remapped`, `output_directory_override`, `filename_format_override`.
  - Written by the worker: `synced_cl`, `lastframerendered`, `lastframerenderedtime`, `gpu_crash_resume`.
- **ExtraInfo0-9**: Monitor columns, display only, never read by the code. `ExtraInfo0` is the temporal sample count, `ExtraInfo9` `submitted_cl`: leave them free in the preset.

A key in the wrong place is ignored without any error: a preset that sets `SyncToSpecificCL` or `P4_workspace_prefix` in its environment must move it to its Extra Info Key Values.


# Branches and deploying to production

- `dev`: day-to-day work.
- `main`: exactly what is submitted in the production P4 depot. Merge `dev` into it when a version is ready.

Production is updated one way only, from `main` to P4, with `scripts/sync_to_p4.ps1`. Never edit the plugins directly in the P4 workspace.

```powershell
git checkout main; git merge dev; git push
.\scripts\sync_to_p4.ps1                     # dry run: checks + plan, touches nothing
.\scripts\sync_to_p4.ps1 -Apply              # fills a NEW pending CL (never submits)
# review and submit the CL in P4V, then:
.\scripts\sync_to_p4.ps1 -TagSubmitted 1712  # tags the commit p4-CL1712 and pushes the tag
```

The script ships only git-tracked files, installs `PreBuiltBinaries/<EngineVersion>` as `Binaries`, and refuses to run if prod was edited outside git since the last `p4-CL*` tag (`-AllowDrift` overrides), or if a plugin's `Source/` changed after its `PreBuiltBinaries` (`-AllowStaleBinaries` overrides).

To test the Deadline side (`JobPreLoad.py`, `UnrealEngine5.py`, ...) on a farm that also runs production jobs, deploy it as a separate Deadline plugin:

```powershell
.\scripts\deploy_deadline_dev.ps1           # working tree -> <repository>/custom/plugins/UnrealEngine5Dev
```

Only jobs whose Deadline job preset sets **Plugin** to `UnrealEngine5Dev` run that copy. Add a machine allowlist in the same preset to keep test jobs on your own Worker.


# How to use

Once Unreal setup is done (and restarted), in the **MRQ** window there will now be a **Deadline** section for each job that are added to it.
There you can specify some info for the job, and there is an entry for a **Deadline job preset** asset that is mandatory.
To override any values of the preset, you need to check the checkbox in front of the option you're overriding.
**MoviePipelineGameOverrideSetting** needs to be enabled in the configuration of the job.

Click on **Render remote** and that's it :)


# List of changes

- Forced command line argument `-renderoffscreen`, as typical renderfarm worker do not have the UI setup and live render preview is unnecessary.
- Removed the command line mode (`CommandLineMode`, Unreal in `-game` with a manifest file) and its **Submit Movie Render Queue Asset** menu: it had no Python (no Kitsu callbacks, no map preload), applied no graph override, frame range or GPU crash resume, and reported failures through the exit code only. Every job renders in the editor.
- Added a **JobPreLoad** that will sync the local Perforce repository based on the CL provided for the job.
- Added a custom pre-script to run at Unreal's opening. Allows to do some process before the render tasks starts.
- Added the sequence's map path to the job info so the pre-script can open it early, preventing issues with unfinished loading of meshes or textures (that would not even load at later frames or with lots of warmup frames).
- Fixed applying overrides to the Unreal job configuration (like output directory and filename). Modifying the configuration does not dirty it, and Unreal ignores overrides if it is not dirty.
- Prevented processing of disabled Unreal jobs.
- Fixed a crash when no job preset was assigned.
- Added Perforce info to the job (Extra Info Key Values, see "Job data").
- Fixed job's username.
- Added a raise when **MoviePipelineGameOverrideSetting** is not enabled. It is usually wanted when rendering cinematic quality images.
- Added a check for identical shots within the same sequence.
- Added a raise when output directory override is not provided, as the default is usually local to the project.
- Included output directory and filename in the job info, enabling Deadline to provide the associated right-click context options.
- Added the plugin info entry **OverrideExistingOutput** ("Override Existing Output" in the job's UnrealEngine5 settings, **True** by default). This ensures that if a task starts rendering images and then crashes, it will override the existing images upon restarting instead of creating new images with number offsets. (`shot_name.f1001.png(2)`)
- Forced command line resolution arguments to match the actual output image resolution. Also added `-ForceRes` to force Unreal to consider this resolution instead of the rendering machine's resolution.
- Added a method to pack multiple shots into one task based on **ChunkSize** to balance frames-per-task in the job.
- Added frame ranges to tasks. In case of shot packing, frame ranges will not reflect the actual frame start and end, but will reflect the real frame count.
- Enabled frame timeout (when frame ranges are provided), to allow a more precise timeout management as shots can have widely different frame counts to render.
- Fixed **DeadlineJobPreset** overrides to not apply correctly when adding a job in the **MRQ**.
- Fixed **DefaultJobPreset** to not be saved in the config.
- Fixed auxilliary files key syntax in the Deadline command for sending jobs.
- Improved log handling with optionnal regex in plugin infos. Allows to indicate custom regex to parse Unreal logs for progress, warnings and errors. Progress logging can be achieved using a custom executor in Unreal.


# Further improvements ideas

- Add support for Perforce streams. Current implementation is not ideal because changes needed for a render have to be pushed on the main branch, making them the new default for every users while sender may just want to "test" stuff without officially pushing that to the others.

- Add an option to write images local to the farmer, then copy to final destination on task end.

- Implement an override to overwrite output frame range.

- Run a post job script that would compute frames render time and write a file that could be used for statistics.

- Make an option to opt-in/out of shot packing.


- Make Perforce dependency and CL syncing optionnal. Current implementation assumes workers need to sync their local workspace.
