import unreal
import os

from deadline_rpc.client import RPCClient


unreal.log_warning("running custom_unreal_prescript")


def preload_map_enabled():
    """
    PreloadMap plugin info entry of the job (UnrealEngine5 settings in the Monitor's
    job properties, or the job preset's Plugin Info), false by default. Read from the
    Deadline plugin through the RPC, whose server is up before Unreal starts.
    """
    port = os.environ.get("DEADLINE_RPC_PORT")
    if not port:
        return False
    preload = RPCClient(port=int(port)).proxy.get_plugin_info_entry("PreloadMap", "false")
    return str(preload).strip().lower() in ("true", "1")


map_path = os.environ.get("UEMAP_PATH")

# Opt-in (PreloadMap): opens the job's map (UEMAP_PATH) in the editor before the
# render, for maps whose meshes or textures would otherwise not finish loading.
# PIE renders the job's map either way, but when it is already open in the editor
# PIE duplicates that editor world, with whatever editor-side logic changed in it
# after loading (see README, "Map preload"). Off, PIE loads the map from disk.
if map_path:
    if preload_map_enabled():
        unreal.log_warning(f"preloading map {map_path}")
        unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level(map_path)
    else:
        unreal.log_warning(f"map not preloaded (PreloadMap plugin info off): {map_path}")


unreal.log_warning("custom_unreal_prescript finished")
