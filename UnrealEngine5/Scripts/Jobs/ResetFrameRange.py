from Deadline.Scripting import MonitorUtils, RepositoryUtils

def __main__(*args):
    for job in MonitorUtils.GetSelectedJobs():
        original = job.GetJobExtraInfoKeyValue("original_frame_range")
        if not original:
            print(f"{job.JobName}: no original_frame_range stored, skipping")
            continue
        RepositoryUtils.SetJobFrameRange(job, original, job.JobFramesPerTask)
        RepositoryUtils.SaveJob(job)
        print(f"{job.JobName}: frame range reset to {original}")