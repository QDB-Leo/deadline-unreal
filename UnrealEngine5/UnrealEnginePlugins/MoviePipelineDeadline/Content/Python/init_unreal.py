# Copyright Epic Games, Inc. All Rights Reserved

# Third-party
import unreal

import remote_executor
import mrq_cli

plugin_name = "MoviePipelineDeadline"


# The asset registry may not be fully loaded by the time this is called,
# warn the user that attempts to look assets up may fail
# unexpectedly.
# Look for a custom commandline start key `-waitonassetregistry`. This key
# is used to trigger a synchronous wait on the asset registry to complete.
# This is useful in commandline states where you explicitly want all assets
# loaded before continuing.
asset_registry = unreal.AssetRegistryHelpers.get_asset_registry()
if asset_registry.is_loading_assets() and ("-waitonassetregistry" in unreal.SystemLibrary.get_command_line().split()):
    unreal.log_warning(
        f"Asset Registry is still loading. The {plugin_name} plugin will "
        f"be loaded after the Asset Registry is complete."
    )

    asset_registry.wait_for_completion()
    unreal.log(f"Asset Registry is complete. Loading {plugin_name} plugin.")
