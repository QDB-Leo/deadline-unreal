# Run by clone_job_to_dev.ps1 through `deadlinecommand -ExecuteScript`, in Deadline's
# Python: writes the submission files (job info, plugin info) of a copy of a job.
# Reads its settings from the environment, as -ExecuteScript passes no arguments.
import json
import os

from Deadline.Scripting import RepositoryUtils

# Written on the job by the worker during its render (JobPreLoad, UnrealEngine5.py):
# the copy starts from scratch, without the original's GPU crash resume
WORKER_KEYS = ("synced_cl", "lastframerendered", "lastframerenderedtime", "gpu_crash_resume")


def key_values(job, keys_getter, value_getter):
    return {key: value_getter(key) for key in keys_getter() if key}


def __main__(*args):
    settings = json.loads(os.environ["CLONE_JOB_SETTINGS"])
    job = RepositoryUtils.GetJob(settings["job_id"], True)
    if job is None:
        raise RuntimeError(f"No job {settings['job_id']} in the repository")

    job_info = {
        "Plugin": settings["plugin"],
        "Name": f"{job.JobName} [{settings['plugin']}]",
        "BatchName": settings["batch_name"],
        "Comment": f"Copy of {job.JobId} ({job.JobPlugin})",
        "Frames": settings["frames"] or job.JobFrames,
        "ChunkSize": str(job.JobFramesPerTask),
        "Priority": str(job.JobPriority),
        "Pool": job.JobPool,
        "Group": job.JobGroup,
        "TaskTimeoutSeconds": str(job.JobTaskTimeoutSeconds),
        "EnableFrameTimeouts": str(job.JobEnableFrameTimeouts),
        "OnTaskTimeout": str(job.JobOnTaskTimeout),
        "Whitelist": settings["worker"],
        "MachineLimit": "1",
    }
    if settings["suspended"]:
        job_info["InitialStatus"] = "Suspended"
    for index, directory in enumerate(job.JobOutputDirectories):
        job_info[f"OutputDirectory{index}"] = directory
    for index, filename in enumerate(job.JobOutputFileNames):
        job_info[f"OutputFilename{index}"] = filename
    for index in range(10):
        value = getattr(job, f"JobExtraInfo{index}")
        if value:
            job_info[f"ExtraInfo{index}"] = value

    extra_info = key_values(job, job.GetJobExtraInfoKeys, job.GetJobExtraInfoKeyValue)
    for key in WORKER_KEYS:
        extra_info.pop(key, None)
    environment = key_values(job, job.GetJobEnvironmentKeys, job.GetJobEnvironmentKeyValue)
    plugin_info = key_values(job, job.GetJobPluginInfoKeys, job.GetJobPluginInfoKeyValue)

    # Overrides: an empty value removes the key
    for target, overrides in ((environment, settings["environment"]), (plugin_info, settings["plugin_info"])):
        for key, value in overrides.items():
            if value:
                target[key] = value
            else:
                target.pop(key, None)

    for index, (key, value) in enumerate(extra_info.items()):
        job_info[f"ExtraInfoKeyValue{index}"] = f"{key}={value}"
    for index, (key, value) in enumerate(environment.items()):
        job_info[f"EnvironmentKeyValue{index}"] = f"{key}={value}"

    # One line per key, like deadline_command.py writes them at submission
    for path, values in ((settings["job_file"], job_info), (settings["plugin_file"], plugin_info)):
        with open(path, "w", encoding="utf-8") as f:
            for key, value in values.items():
                f.write(f"{key}={str(value).replace(chr(10), '').replace(chr(13), '')}\n")

    print(f"clone files written for {job.JobId} ({job.JobName}): {len(extra_info)} extra info, "
          f"environment {sorted(environment)}, plugin info {sorted(plugin_info)}")
