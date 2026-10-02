<#
.SYNOPSIS
    Copies the Deadline side of this repo (UnrealEngine5/) into the repository's
    custom plugins as a separate dev plugin, UnrealEngine5Dev by default.

.DESCRIPTION
    Lets you test changes to JobPreLoad.py, UnrealEngine5.py, ... on the farm without
    touching the UnrealEngine5 plugin that production jobs use. Only jobs submitted
    with Plugin=<PluginName> run this code (set it in the Deadline job preset).

    Copies the working tree, uncommitted changes included: tracked and untracked
    non-ignored files under UnrealEngine5/, except the Unreal plugins, the job
    scripts and the README. UnrealEngine5.* files are renamed <PluginName>.*, as
    Deadline expects. Files in the target that are no longer in the source are
    deleted, so the target mirrors the source.

    The repository root comes from `deadlinecommand -GetRepositoryRoot`.

.EXAMPLE
    .\scripts\deploy_deadline_dev.ps1
    .\scripts\deploy_deadline_dev.ps1 -WhatIf     # prints what would change
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$PluginName = 'UnrealEngine5Dev',
    [string]$DeadlineCommand = 'C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe'
)

$ErrorActionPreference = 'Stop'

if ($PluginName -eq 'UnrealEngine5') {
    throw "UnrealEngine5 is the production plugin: deploy it on purpose, not with this script."
}

$repoRoot = Split-Path $PSScriptRoot -Parent
$sourceRoot = Join-Path $repoRoot 'UnrealEngine5'
$excluded = @('UnrealEnginePlugins/*', 'Scripts/*', 'README.md')

$deadlineRoot = (& $DeadlineCommand -GetRepositoryRoot | Select-Object -First 1).Trim()
if (-not $deadlineRoot -or -not (Test-Path $deadlineRoot)) { throw "Deadline repository not found: '$deadlineRoot'" }
$target = Join-Path $deadlineRoot "custom\plugins\$PluginName"

# relative source path -> relative target path
$files = [ordered]@{}
foreach ($rel in (git -C $sourceRoot ls-files --cached --others --exclude-standard)) {
    if ($excluded | Where-Object { $rel -like $_ }) { continue }
    if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $rel))) { continue }   # deleted, not committed yet
    $files[$rel] = $rel -replace '^UnrealEngine5\.', "$PluginName."
}
if (-not $files.Contains('UnrealEngine5.py')) { throw "UnrealEngine5/UnrealEngine5.py not found in $sourceRoot" }

Write-Host "deploying $($files.Count) files to $target"
$copied = 0
foreach ($rel in $files.Keys) {
    $src = Join-Path $sourceRoot $rel
    $dst = Join-Path $target $files[$rel]
    if ((Test-Path -LiteralPath $dst) -and ((Get-FileHash -LiteralPath $src).Hash -eq (Get-FileHash -LiteralPath $dst).Hash)) { continue }
    if ($PSCmdlet.ShouldProcess($dst, 'copy')) {
        New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
        Copy-Item -LiteralPath $src -Destination $dst -Force
        Write-Host "  copied  $($files[$rel])" -ForegroundColor Green
    }
    $copied++
}

$deleted = 0
if (Test-Path $target) {
    $wanted = @($files.Values | ForEach-Object { $_.Replace('/', '\') })
    foreach ($file in Get-ChildItem $target -Recurse -File) {
        $rel = $file.FullName.Substring($target.Length + 1)
        if ($rel -match '(^|\\)__pycache__\\' -or $wanted -contains $rel) { continue }
        if ($PSCmdlet.ShouldProcess($file.FullName, 'delete')) {
            Remove-Item -LiteralPath $file.FullName -Force
            Write-Host "  deleted $rel" -ForegroundColor Yellow
        }
        $deleted++
    }
}

Write-Host "$PluginName`: $copied copied, $deleted deleted, $($files.Count - $copied) unchanged." -ForegroundColor Cyan
