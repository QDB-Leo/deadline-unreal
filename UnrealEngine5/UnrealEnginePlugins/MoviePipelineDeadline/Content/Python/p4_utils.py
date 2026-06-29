import os
import configparser
from P4 import P4, P4Exception


def _log(logger, level, msg):
    """Logger neutre : marche sous Unreal, Deadline, ou standalone."""
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
    """Construit un objet P4 DÉCONNECTÉ depuis le .ini (ou l'environnement)."""
    settings = (
        get_perforce_settings_from_unreal_ini(project_root, logger)
        if project_root else {"P4PORT": "", "P4USER": ""}
    )
    p4 = P4()
    if settings["P4PORT"]:
        p4.port = settings["P4PORT"]
    else:
        _log(logger, "warning", "P4PORT absent, fallback environnement P4.")
    if settings["P4USER"]:
        p4.user = settings["P4USER"]
    else:
        _log(logger, "warning", "P4USER absent, fallback environnement P4.")
    return p4


def verify_p4_ticket(p4):
    """Valide qu'un ticket existe pour ce port=user. Lève RuntimeError actionnable."""
    try:
        p4.connect()
    except P4Exception as err:
        raise RuntimeError(
            f"Serveur P4 injoignable ({p4.user}@{p4.port}) : {err}\n"
            f"Vérifier P4PORT et le réseau."
        )
    try:
        p4.run_login("-s")
    except P4Exception:
        raise RuntimeError(
            f"Pas de ticket P4 valide pour {p4.user}@{p4.port} sur cette machine.\n"
            f"Login manuel requis : `p4 login` (P4PORT={p4.port}, P4USER={p4.user})."
        )
    finally:
        if p4.connected():
            p4.disconnect()


def get_latest_submitted_cl(p4, logger=None):
    try:
        p4.connect()
        return p4.run_changes("-s", "submitted", "-m", "1")[0]["change"]
    except P4Exception as e:
        _log(logger, "error", f"Erreur P4 (get_latest_submitted_cl): {e}")
        for err in p4.errors:
            _log(logger, "error", f"  {err}")
        return None
    finally:
        if p4.connected():
            p4.disconnect()