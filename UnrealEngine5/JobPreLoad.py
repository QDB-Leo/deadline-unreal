#!/usr/bin/env python3

import os
import socket 
import sys
from pathlib import Path

from Deadline.Scripting import RepositoryUtils
from P4 import P4, P4Exception

#We use a pattern to find the workspace, that needs to match to get the correct client and root for the p4 sync
WORKSPACE_PATTERN = "{user}_{machine}_{prefix}"


def __main__(deadline_plugin):
    # Add the location of the plugin package to the system path so the plugin
    # can import supplemental modules if it needs to
    plugin_path = Path(__file__)
    if plugin_path.parent not in sys.path:
        sys.path.append(plugin_path.parent.as_posix())

    # get the job
    job = deadline_plugin.GetJob()

    # initialize P4
    p4 = P4()
    p4.port = job.GetJobEnvironmentKeyValue("P4_PORT")
    p4.user = job.GetJobEnvironmentKeyValue("P4_USER")

    # Ticket-based auth only — no hardcoded password.
    # The farm account must be logged in once, outside of this code:
    # p4 login   (ideally in a group with a long timeout so it doesn't re-expire)
    # We do NOT set p4.password: P4Python will use the cached ticket (.p4tickets).


    # sync P4 local workspace for render
    with p4.connect() as p4:
        deadline_plugin.LogInfo(f"P4 auth attempt: port={p4.port!r} user={p4.user!r}")
        deadline_plugin.LogInfo(f"Home: {os.path.expanduser('~')!r}  P4TICKETS={os.environ.get('P4TICKETS')!r}")
        try:
            p4.run_login("-s")
        except P4Exception:
            deadline_plugin.FailRender(
                f"No valid P4 ticket for {p4.user}@{p4.port} on this worker.\n"
                f"Service login required: `p4 login -a` under the farm account "
                f"(P4PORT={p4.port}, P4USER={p4.user})."
            )
            return

        # get project prefix
        workspace_prefix = job.GetJobEnvironmentKeyValue("P4_workspace_prefix")

        if not workspace_prefix:
            deadline_plugin.FailRender(
                "Workspace prefix not provided, cannot synchronize P4 workspace for render."
            )
            return

        # find perforce workspace by matching a pattern, for example prefix_user_machine
        workspace_pattern = WORKSPACE_PATTERN.format(prefix=workspace_prefix, user=p4.user, machine=socket.gethostname())

        found_workspaces = p4.run_clients("-u", p4.user, "-E", workspace_pattern)

        if len(found_workspaces) != 1:
            deadline_plugin.FailRender(
                f"Found {len(found_workspaces)} workspaces for pattern {workspace_pattern}, cannot determine workspace."
                f"\nWorkspace pattern is {workspace_pattern}. Workspace prefix should be project name"
            )
            return

        workspace = found_workspaces[0]

        p4.client = workspace['client']

        # add ProjectRoot to process env so that job can retrieve it
        project_root = workspace['Root']
        deadline_plugin.SetProcessEnvironmentVariable("ProjectRoot", project_root)

        # Optional subpath to synchronize (relative to the workspace root),
        # to sync only the UE project and not the neighboring work files.
        # Set once per project in the DeadlineJobPreset (ExtraInfoKeyValue "SyncSubPath") -> DeadlineJobPreset Level (no per-job override)
        sync_subpath = (job.GetJobExtraInfoKeyValue("SyncSubPath") or "").strip()
        if sync_subpath:
            # normalize the separators and strip any leading slash
            sync_subpath = sync_subpath.replace("/", "\\").lstrip("\\")
            sync_root = os.path.join(project_root, sync_subpath)
            deadline_plugin.LogInfo(f"Sync limited to subpath: {sync_root}")
        else:
            sync_root = project_root
            deadline_plugin.LogInfo(f"Sync of the full root: {sync_root}")

        # local CL #have is latest version of files synced in workspace.. #head is latest version in depot
        server_cl_dict = p4.run("changes", "-m", "1", f"{sync_root}\\...#head")
        server_cl = server_cl_dict[0]['change'] if server_cl_dict else None
        deadline_plugin.LogInfo(f"Server CL is {server_cl}")

        local_cl_dict = p4.run("changes", "-m", "1", f"{sync_root}\\...#have")
        local_cl = local_cl_dict[0]['change'] if local_cl_dict else None
        deadline_plugin.LogInfo(f"Local CL is {local_cl}")

        sync_to_specific_cl = (job.GetJobEnvironmentKeyValue("SyncToSpecificCL") or "").lower() in ("true", "1", "yes", "t", "y") #environmentkeyvalue, per-job override
        if sync_to_specific_cl:
            wanted_cl = job.GetJobEnvironmentKeyValue("P4_CL")
            if not wanted_cl:
                deadline_plugin.FailRender("SyncToSpecificCL is true but no wanted CL provided in job's P4_CL environment variable.")
                return
            sync_target = wanted_cl
        else:
            sync_target = server_cl

        deadline_plugin.LogInfo(f"Shot wanted CL is {sync_target}")

        if sync_to_specific_cl: # have/target comparison unreliable → always sync
            sync = True   
        else:
            sync = (not local_cl) or (local_cl != server_cl)

        if sync:
            try:
                synced_cl = p4.run("sync", f"{sync_root}\\...@{sync_target}")
                deadline_plugin.LogInfo(f"synced to CL {synced_cl}")
            except P4Exception as e:
                fail = True
                if e.warnings:
                    # It is possible that P4 will consider that the repo is on the correct CL
                    # while `p4 changes -m 1 root\\...#have` returns another CL number
                    # For now, ignore the warning and continue the job
                    warning = e.warnings[0]
                    if "file(s) up-to-date." in warning:
                        fail = False
                        deadline_plugin.LogWarning(f"Current CL is not the same as wanted CL but P4 says that repo is correctly synced: {e}")
                if fail:
                    deadline_plugin.FailRender(f"Errors: {e.errors} \nWarnings: {e.warnings}")
                    return

#__________________Output directory override____________________

    # update job output directory
    output_directory_override = job.GetJobExtraInfoKeyValue("output_directory_override")
    if output_directory_override and os.path.isdir(output_directory_override):
        RepositoryUtils.SetJobOutputDirectories(job, [output_directory_override])

    RepositoryUtils.SaveJob(job)
