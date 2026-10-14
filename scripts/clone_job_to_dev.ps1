<#
.SYNOPSIS
    Submits a copy of an existing Deadline job (a production one, typically) on the
    dev plugin, kept on one Worker.

.DESCRIPTION
    To render a real shot with the Deadline side under test (deploy_deadline_dev.ps1
    first) without submitting it again from Unreal. Deadline can't change the plugin
    of a job (SetJobSetting PluginName doesn't stick), so the copy is a new job, built
    from the original by clone_job_to_dev.py in Deadline's Python:

    - same frames, chunk size, timeouts, priority, pool, group, output, ExtraInfo0-9,
      extra info, environment and plugin info;
    - Plugin = -Plugin, BatchName = -BatchName, allowlist = -Worker (machine limit 1),
      name suffixed with the plugin;
    - without the keys the worker writes during a render (synced_cl,
      lastframerendered*, gpu_crash_resume) or the notification targets.

    The original job is not modified.

    The Unreal side is NOT the one of this repo: the Worker opens the project of its
    own P4 workspace, synced by JobPreLoad, so it runs the Unreal plugins submitted in
    P4. A plugin info entry only read by Unreal code not submitted yet is ignored.

    The render does everything a production render does, Kitsu publish included.

.EXAMPLE
    .\scripts\clone_job_to_dev.ps1 6ac8e1aa5642d6d6e5df69e2
    .\scripts\clone_job_to_dev.ps1 6ac8e1aa5642d6d6e5df69e2 -Environment @{ UEMAP_PATH = '' }   # no map preload
    .\scripts\clone_job_to_dev.ps1 6ac8e1aa5642d6d6e5df69e2 -PluginInfo @{ PreloadMap = 'false' } -Frames 0-9 -Suspended
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$JobId,
    [string]$Plugin = 'UnrealEngine5Dev',
    [string]$Worker = $env:COMPUTERNAME,
    [string]$BatchName = 'Testbed',
    [string]$Frames,
    [hashtable]$PluginInfo = @{},
    [hashtable]$Environment = @{},
    [switch]$Suspended,
    [string]$DeadlineCommand = 'C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe'
)

$ErrorActionPreference = 'Stop'

if ($Plugin -eq 'UnrealEngine5') {
    throw "UnrealEngine5 is the production plugin: the copy is for testing the dev one."
}

$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) "clone_job_$JobId"
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
$jobFile = Join-Path $tempDir 'job_info.job'
$pluginFile = Join-Path $tempDir 'plugin_info.job'

$env:CLONE_JOB_SETTINGS = @{
    job_id      = $JobId
    plugin      = $Plugin
    worker      = $Worker
    batch_name  = $BatchName
    frames      = "$Frames"
    suspended   = [bool]$Suspended
    plugin_info = $PluginInfo
    environment = $Environment
    job_file    = $jobFile
    plugin_file = $pluginFile
} | ConvertTo-Json -Compress

try {
    & $DeadlineCommand -ExecuteScriptNoGui (Join-Path $PSScriptRoot 'clone_job_to_dev.py')
    if (-not (Test-Path $jobFile) -or -not (Test-Path $pluginFile)) { throw "clone_job_to_dev.py wrote no submission files, see above." }

    $output = & $DeadlineCommand -SubmitJob $jobFile $pluginFile
    $newId = ($output | Select-String '^JobID=(\w+)').Matches | ForEach-Object { $_.Groups[1].Value } | Select-Object -First 1
    if (-not $newId) { $output; throw "Submission failed, see above." }
    Write-Host "job $newId submitted: copy of $JobId on $Plugin, allowlist $Worker, batch '$BatchName'$(if ($Suspended) { ', suspended' })"
    Write-Host "delete it once done: & '$DeadlineCommand' -DeleteJob $newId"
    $newId
} finally {
    Remove-Item Env:CLONE_JOB_SETTINGS -ErrorAction SilentlyContinue
    Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}
