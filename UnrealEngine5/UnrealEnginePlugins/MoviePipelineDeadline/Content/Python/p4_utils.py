import os
import configparser
from P4 import P4, P4Exception


def _log(logger, level, msg):
    """Logs through logger (Unreal, Deadline) or print."""
    if logger:
        getattr(logger, level, logger.info if hasattr(logger, "info") else print)(msg)
    else:
        print(f"[p4_utils:{level}] {msg}")


def get_perforce_settings_from_unreal_ini(project_root, logger=None):
    ini_path = os.path.join(project_root, "Saved", "Config", "WindowsEditor", "SourceControlSettings.ini")
    settings = {"P4PORT": "", "P4USER": "", "P4CLIENT": ""}

    if not os.path.exists(ini_path):
        _log(logger, "error", f"Missing Unreal Perforce config: {ini_path}")
        return settings

    config = configparser.ConfigParser()
    config.read(ini_path)
    section = 'PerforceSourceControl.PerforceSourceControlSettings'
    if section not in config:
        _log(logger, "error", f"Missing section [{section}] in {ini_path}")
        return settings

    settings["P4PORT"] = config[section].get('Port') or ""
    settings["P4USER"] = config[section].get('UserName') or ""
    settings["P4CLIENT"] = config[section].get('Workspace') or ""
    return settings


def get_p4(project_root=None, logger=None):
    """A DISCONNECTED P4 object, set from Unreal's source control settings (or the P4 environment)."""
    settings = (
        get_perforce_settings_from_unreal_ini(project_root, logger)
        if project_root else {"P4PORT": "", "P4USER": ""}
    )
    p4 = P4()
    if settings["P4PORT"]:
        p4.port = settings["P4PORT"]
    else:
        _log(logger, "warning", "No P4PORT in the settings, using the P4 environment.")
    if settings["P4USER"]:
        p4.user = settings["P4USER"]
    else:
        _log(logger, "warning", "No P4USER in the settings, using the P4 environment.")
    # The workspace too: commands on the project's local paths need it
    if settings.get("P4CLIENT"):
        p4.client = settings["P4CLIENT"]
    return p4


def verify_p4_ticket(p4):
    """Checks there is a ticket for this port and user. Raises a RuntimeError saying what to do."""
    try:
        p4.connect()
    except P4Exception as err:
        raise RuntimeError(
            f"P4 server unreachable ({p4.user}@{p4.port}): {err}\n"
            f"Check P4PORT and the network."
        )
    try:
        p4.run_login("-s")
    except P4Exception:
        raise RuntimeError(
            f"No valid P4 ticket for {p4.user}@{p4.port} on this machine.\n"
            f"Log in first: `p4 login` (P4PORT={p4.port}, P4USER={p4.user})."
        )
    finally:
        if p4.connected():
            p4.disconnect()


def get_latest_submitted_cl(p4, logger=None, path=None):
    """
    Latest submitted CL under path (e.g. the project directory, through the workspace),
    or of the whole server without path.
    """
    try:
        p4.connect()
        args = ["-s", "submitted", "-m", "1"]
        if path:
            args.append(os.path.join(path, "..."))
        return p4.run_changes(*args)[0]["change"]
    except P4Exception as e:
        _log(logger, "error", f"P4 error (get_latest_submitted_cl): {e}")
        for err in p4.errors:
            _log(logger, "error", f"  {err}")
        return None
    finally:
        if p4.connected():
            p4.disconnect()

def get_opened_files(p4, path, logger=None):
    """
    Local paths of the files opened (checked out, added, deleted) in the workspace under
    path: work the farm won't render, since it syncs the depot. None if P4 can't tell.
    """
    try:
        p4.connect()
        return [f.get("clientFile") or f.get("depotFile") for f in p4.run_opened(os.path.join(path, "..."))]
    except P4Exception as e:
        _log(logger, "warning", f"P4 error (get_opened_files): {e}")
        return None
    finally:
        if p4.connected():
            p4.disconnect()
