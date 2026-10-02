<#
.SYNOPSIS
    One-way sync of the Unreal plugins from this repo (branch `main`) into the
    Perforce workspace used by production.

.DESCRIPTION
    git (main) -> P4. Never the other way, and never submits.

    Default run is a DRY RUN: it checks everything and prints the plan
    (add / edit / delete per file) without touching the P4 workspace.

    With -Apply it creates a NEW pending changelist, opens the files in it,
    copies them over and reverts anything unchanged. Review the CL in P4V and
    submit it yourself, then run with -TagSubmitted <CL> to tag the git commit.

    Mapping rules:
      - Only files tracked by git are shipped (no Intermediate/, __pycache__, ...).
      - PreBuiltBinaries/<EngineVersion>/* is shipped as Binaries/*.
      - Files matching $DevOnlyPatterns never ship (and are deleted from P4 if present).
      - Files under Binaries/ that exist only in P4 are left alone (e.g. .exp).

    Safety checks (all abort the run):
      - git: must be on -Branch, clean, and equal to origin/<Branch>.
      - binaries: PreBuiltBinaries/<EngineVersion> committed after the plugin's
            last Source/ change (-AllowStaleBinaries to proceed anyway).
      - p4: logged in, workspace synced to head for the plugin folders,
            no plugin file already opened in another changelist,
            no unopened local edits in the workspace.
      - drift: if a previous `p4-CL*` tag exists, any P4 difference that git
            did not introduce since that tag means prod was edited outside
            git. Use -AllowDrift to proceed anyway (the sync overwrites it).

.EXAMPLE
    .\scripts\sync_to_p4.ps1                    # dry run, prints the plan
    .\scripts\sync_to_p4.ps1 -Apply             # fills a new pending CL
    .\scripts\sync_to_p4.ps1 -TagSubmitted 1712 # after submit: tag + push p4-CL1712
#>
[CmdletBinding(DefaultParameterSetName = 'Sync')]
param(
    [Parameter(ParameterSetName = 'Sync')]
    [switch]$Apply,

    [Parameter(ParameterSetName = 'Sync')]
    [switch]$AllowDrift,

    [Parameter(ParameterSetName = 'Sync')]
    [switch]$AllowStaleBinaries,

    [Parameter(ParameterSetName = 'Tag', Mandatory = $true)]
    [int]$TagSubmitted,

    [string]$Target = 'U:\WS_RSystem\RSystem\Plugins',
    [string]$Branch = 'main',
    [string]$EngineVersion = '5.7',
    [string[]]$Plugins = @('MoviePipelineDeadline', 'UnrealDeadlineService')
)

$ErrorActionPreference = 'Stop'

# Tracked in git but must never reach production (repo-relative, wildcard).
$DevOnlyPatterns = @(
    '*/Content/Python/tools/*',
    '*/Content/Python/unreal_stub_*.py'
)
$TextExtensions = @('.py', '.ini', '.uplugin', '.cs', '.cpp', '.h', '.json', '.md', '.txt', '.modules')
$TagPrefix = 'p4-CL'
$DescriptionMarker = 'deadline-unreal'


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

function Invoke-Native {
    # Runs a native exe without PowerShell 5.1 turning stderr into exceptions.
    param([string]$Exe, [string[]]$Arguments, [string[]]$InputLines)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # Piped input gets a UTF-8 BOM when the console encoding is UTF-8 (p4 then
    # reads the first spec field as '﻿Change'): pipe it without one.
    $prevOutput, $prevInput = $OutputEncoding, [Console]::InputEncoding
    try {
        if ($InputLines) {
            $noBom = New-Object System.Text.UTF8Encoding $false
            $OutputEncoding = $noBom
            [Console]::InputEncoding = $noBom
            $out = $InputLines | & $Exe @Arguments 2>&1
        }
        else { $out = & $Exe @Arguments 2>&1 }
    }
    finally {
        $ErrorActionPreference = $prev
        $OutputEncoding = $prevOutput
        [Console]::InputEncoding = $prevInput
    }
    $code = $LASTEXITCODE
    $lines = @($out | ForEach-Object { "$_" })
    if ($code -ne 0) { throw "$Exe $($Arguments -join ' ') failed (exit $code):`n$($lines -join "`n")" }
    return $lines
}

function Invoke-Git {
    # Plain function (no param block) so git flags like -a / -m land in $args.
    Invoke-Native -Exe 'git' -Arguments (@('-C', $RepoRoot, '-c', 'core.quotepath=off') + $args)
}

function Invoke-P4 {
    # Runs `p4 -s -ztag`, throws on any `error:` line and returns tagged records
    # plus plain info/warning text.
    param([string[]]$P4Args, [string[]]$InputLines)
    $lines = Invoke-Native -Exe 'p4' -Arguments (@('-s', '-ztag') + $P4Args) -InputLines $InputLines
    $errors = @($lines | Where-Object { $_ -like 'error:*' })
    if ($errors) { throw "p4 $($P4Args -join ' ') failed:`n$($errors -join "`n")" }

    $records = New-Object System.Collections.Generic.List[hashtable]
    $messages = New-Object System.Collections.Generic.List[string]
    $current = $null
    foreach ($line in $lines) {
        if ($line -match '^info1: (\S+) ?(.*)$') {
            $key, $value = $Matches[1], $Matches[2]
            if ($null -eq $current -or $current.ContainsKey($key)) {
                $current = @{}
                $records.Add($current)
            }
            $current[$key] = $value
        }
        elseif ($line -match '^(info|warning|text): ?(.*)$') {
            $messages.Add($Matches[2])
        }
    }
    [pscustomobject]@{ Records = $records; Messages = $messages }
}

function Invoke-P4FileList {
    # Runs a p4 command on many local paths through `-x listfile`.
    param([string[]]$P4Args, [string[]]$Paths)
    if (-not $Paths) { return }
    $list = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllLines($list, $Paths)
        $null = Invoke-P4 -P4Args (@('-x', $list) + $P4Args)
    }
    finally { Remove-Item $list -ErrorAction SilentlyContinue }
}

function Test-SameContent {
    param([string]$A, [string]$B)
    if ((Get-FileHash $A -Algorithm MD5).Hash -eq (Get-FileHash $B -Algorithm MD5).Hash) { return $true }
    if ($TextExtensions -notcontains [System.IO.Path]::GetExtension($A).ToLowerInvariant()) { return $false }
    # Same text, different line endings: not a real change.
    $ta = [System.IO.File]::ReadAllText($A) -replace "`r`n", "`n"
    $tb = [System.IO.File]::ReadAllText($B) -replace "`r`n", "`n"
    return $ta -ceq $tb
}

function Test-DevOnly {
    param([string]$RepoPath)
    foreach ($pattern in $DevOnlyPatterns) { if ($RepoPath -like $pattern) { return $true } }
    return $false
}

function Write-Section { param([string]$Text) Write-Host "`n== $Text" -ForegroundColor Cyan }
function Fail { param([string]$Text) Write-Host "`nABORT: $Text" -ForegroundColor Red; exit 1 }


$RepoRoot = @(Invoke-Native -Exe 'git' -Arguments @('-C', $PSScriptRoot, 'rev-parse', '--show-toplevel'))[0]
$PluginsRepoDir = 'UnrealEngine5/UnrealEnginePlugins'


# --------------------------------------------------------------------------
# -TagSubmitted: tag the synced commit once the CL is submitted
# --------------------------------------------------------------------------

if ($PSCmdlet.ParameterSetName -eq 'Tag') {
    $change = (Invoke-P4 -P4Args @('describe', '-s', "$TagSubmitted")).Records[0]
    if (-not $change) { Fail "CL $TagSubmitted not found." }
    if ($change.status -ne 'submitted') { Fail "CL $TagSubmitted is '$($change.status)', not submitted." }
    if ($change.desc -notmatch "$DescriptionMarker $Branch@([0-9a-f]{40})") {
        Fail "CL $TagSubmitted was not created by this script (no '$DescriptionMarker $Branch@<sha>' in its description)."
    }
    $sha = $Matches[1]
    $tag = "$TagPrefix$TagSubmitted"
    Invoke-Git tag -a $tag $sha -m "Submitted to P4 as CL $TagSubmitted" | Out-Null
    Invoke-Git push origin $tag | Out-Null
    Write-Host "Tagged $($sha.Substring(0, 7)) as $tag and pushed it to origin." -ForegroundColor Green
    exit 0
}


# --------------------------------------------------------------------------
# git checks
# --------------------------------------------------------------------------

Write-Section 'git'
$currentBranch = @(Invoke-Git rev-parse --abbrev-ref HEAD)[0]
if ($currentBranch -ne $Branch) { Fail "on branch '$currentBranch', expected '$Branch'." }
if (Invoke-Git status --porcelain) { Fail "working tree is not clean. Commit or stash first." }
Invoke-Git fetch origin $Branch --quiet | Out-Null
$sha = @(Invoke-Git rev-parse HEAD)[0]
$remoteSha = @(Invoke-Git rev-parse "origin/$Branch")[0]
if ($sha -ne $remoteSha) { Fail "$Branch ($($sha.Substring(0,7))) differs from origin/$Branch ($($remoteSha.Substring(0,7))). Push or pull first." }
$subject = @(Invoke-Git log -1 --format=%s)[0]
Write-Host "$Branch@$($sha.Substring(0, 7))  $subject"

$lastTag = $null
$tags = @(Invoke-Git tag --merged HEAD --list "$TagPrefix*" --sort=-creatordate)
if ($tags) { $lastTag = $tags[0] }
if ($lastTag) {
    $changedInGit = @{}
    foreach ($p in (Invoke-Git diff --name-only $lastTag HEAD -- $PluginsRepoDir)) { $changedInGit[$p] = $true }
    Write-Host "last sync: $lastTag ($($changedInGit.Count) plugin file(s) changed in git since)"
}
else {
    Write-Host "last sync: none (no $TagPrefix* tag) - drift check skipped for this first sync" -ForegroundColor Yellow
}


# Prod loads PreBuiltBinaries, not Source/: they must be rebuilt after any C++ change
foreach ($plugin in $Plugins) {
    $dir = "$PluginsRepoDir/$plugin"
    $sourceTime = @(Invoke-Git log -1 --format=%ct -- "$dir/Source")[0]
    $binariesTime = @(Invoke-Git log -1 --format=%ct -- "$dir/PreBuiltBinaries/$EngineVersion")[0]
    if ($sourceTime -and (-not $binariesTime -or [long]$sourceTime -gt [long]$binariesTime)) {
        $stale = "${plugin}: Source/ changed after PreBuiltBinaries/$EngineVersion. Rebuild (testbed's build.ps1 -UpdatePrebuilt) and commit the binaries."
        if (-not $AllowStaleBinaries) { Fail "$stale Or rerun with -AllowStaleBinaries." }
        Write-Host "-AllowStaleBinaries set: $stale" -ForegroundColor Yellow
    }
}


# --------------------------------------------------------------------------
# p4 checks
# --------------------------------------------------------------------------

Write-Section 'p4'
$p4info = (Invoke-P4 -P4Args @('info')).Records[0]
Write-Host "client $($p4info.clientName) / user $($p4info.userName)"
$null = Invoke-P4 -P4Args @('login', '-s')

$targetSpecs = @($Plugins | ForEach-Object { Join-Path $Target "$_\..." })

$needSync = (Invoke-P4 -P4Args (@('sync', '-n') + $targetSpecs)).Records
if ($needSync.Count) {
    $needSync | ForEach-Object { Write-Host "  $($_.depotFile)" }
    Fail "workspace is not at head for the plugin folders. Run 'p4 sync' on them first."
}

$opened = (Invoke-P4 -P4Args (@('opened') + $targetSpecs)).Records
if ($opened.Count) {
    $opened | ForEach-Object { Write-Host "  CL $($_.change)  $($_.action)  $($_.depotFile)" }
    Fail "plugin files are already opened in pending CL(s). Submit, shelve or revert them first."
}

# `diff -se` prints bare paths (not tagged records) and warnings when there is nothing.
$diffResult = Invoke-P4 -P4Args (@('diff', '-se') + $targetSpecs)
$localEdits = @($diffResult.Records | ForEach-Object { $_.clientFile }) +
              @($diffResult.Messages | Where-Object { $_ -match '^[A-Za-z]:\\|^//' -and $_ -notmatch ' - ' })
if ($localEdits.Count) {
    $localEdits | ForEach-Object { Write-Host "  $_" }
    Fail "workspace has local edits on files that are not opened. Revert them (or check them into git) first."
}

# Files the workspace has, keyed by plugin-relative path (e.g. MoviePipelineDeadline\Content\...).
$p4Files = @{}
foreach ($rec in (Invoke-P4 -P4Args (@('have') + $targetSpecs)).Records) {
    $rel = $rec.path.Substring($Target.TrimEnd('\').Length + 1)
    $p4Files[$rel] = $rec.path
}
Write-Host "$($p4Files.Count) file(s) in P4 under the plugin folders"


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

Write-Section 'plan'
# Files git would ship, keyed by the same plugin-relative path.
$sourceFiles = @{}
$skippedDevOnly = New-Object System.Collections.Generic.List[string]
foreach ($plugin in $Plugins) {
    foreach ($repoPath in (Invoke-Git ls-files -- "$PluginsRepoDir/$plugin")) {
        if (Test-DevOnly $repoPath) { $skippedDevOnly.Add($repoPath); continue }
        $rel = $repoPath.Substring($PluginsRepoDir.Length + 1)
        $prebuilt = "$plugin/PreBuiltBinaries/"
        if ($rel.StartsWith($prebuilt)) {
            $versionPrefix = "$prebuilt$EngineVersion/"
            if (-not $rel.StartsWith($versionPrefix)) { continue }
            $rel = "$plugin/Binaries/" + $rel.Substring($versionPrefix.Length)
        }
        $sourceFiles[$rel.Replace('/', '\')] = [pscustomobject]@{
            RepoPath = $repoPath
            Local    = Join-Path $RepoRoot $repoPath
        }
    }
}

$toAdd = New-Object System.Collections.Generic.List[string]
$toEdit = New-Object System.Collections.Generic.List[string]
$toDelete = New-Object System.Collections.Generic.List[string]
$kept = New-Object System.Collections.Generic.List[string]
$drift = New-Object System.Collections.Generic.List[string]

foreach ($rel in ($sourceFiles.Keys | Sort-Object)) {
    $src = $sourceFiles[$rel]
    $changed = $lastTag -and $changedInGit.ContainsKey($src.RepoPath)
    if (-not $p4Files.ContainsKey($rel)) {
        $toAdd.Add($rel)
        if ($lastTag -and -not $changed) { $drift.Add("missing in P4: $rel") }
    }
    elseif (-not (Test-SameContent $src.Local $p4Files[$rel])) {
        $toEdit.Add($rel)
        if ($lastTag -and -not $changed) { $drift.Add("edited in P4:  $rel") }
    }
}
foreach ($rel in ($p4Files.Keys | Sort-Object)) {
    if ($sourceFiles.ContainsKey($rel)) { continue }
    if ($rel -like '*\Binaries\*') { $kept.Add($rel); continue }
    $toDelete.Add($rel)
    $repoPath = "$PluginsRepoDir/" + $rel.Replace('\', '/')
    $isDevOnly = Test-DevOnly $repoPath
    $deletedInGit = $lastTag -and $changedInGit.ContainsKey($repoPath)
    if ($lastTag -and -not $isDevOnly -and -not $deletedInGit) { $drift.Add("added in P4:   $rel") }
}

foreach ($rel in $toAdd) { Write-Host "  add     $rel" -ForegroundColor Green }
foreach ($rel in $toEdit) { Write-Host "  edit    $rel" -ForegroundColor Yellow }
foreach ($rel in $toDelete) { Write-Host "  delete  $rel" -ForegroundColor Red }
foreach ($rel in $kept) { Write-Host "  keep    $rel  (P4-only binary, left alone)" -ForegroundColor DarkGray }
foreach ($repoPath in $skippedDevOnly) { Write-Host "  skip    $repoPath  (dev-only)" -ForegroundColor DarkGray }
$total = $toAdd.Count + $toEdit.Count + $toDelete.Count
Write-Host "`n$($toAdd.Count) add, $($toEdit.Count) edit, $($toDelete.Count) delete"

if ($drift.Count) {
    Write-Host "`nProd differs from $lastTag in ways git did not introduce (edited outside git):" -ForegroundColor Red
    $drift | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    if (-not $AllowDrift) {
        Fail "drift detected. Bring those changes into git first, or rerun with -AllowDrift to overwrite them."
    }
    Write-Host "-AllowDrift set: the sync will overwrite them." -ForegroundColor Yellow
}

if ($total -eq 0) { Write-Host "`nP4 already matches $Branch@$($sha.Substring(0, 7)). Nothing to do." -ForegroundColor Green; exit 0 }
if (-not $Apply) { Write-Host "`nDry run: nothing was changed. Rerun with -Apply to fill a new pending CL." -ForegroundColor Cyan; exit 0 }


# --------------------------------------------------------------------------
# apply: new pending CL, open, copy, revert unchanged
# --------------------------------------------------------------------------

Write-Section 'apply'
$spec = @(
    'Change: new'
    "Client: $($p4info.clientName)"
    "User: $($p4info.userName)"
    'Status: new'
    'Description:'
    "`t$DescriptionMarker $Branch@$sha"
    "`t$subject"
    "`t(synced by scripts/sync_to_p4.ps1 - do not edit these files in P4, change them in git)"
)
$created = Invoke-P4 -P4Args @('change', '-i') -InputLines $spec
$cl = $null
foreach ($m in $created.Messages) { if ($m -match 'Change (\d+) created') { $cl = $Matches[1] } }
if (-not $cl -and $created.Records.Count -and $created.Records[0].change -match '^\d+$') { $cl = $created.Records[0].change }
if (-not $cl) { Fail "could not create a pending changelist: $($created.Messages -join ' ')" }
Write-Host "created pending CL $cl"

$abs = { param($rel) Join-Path $Target $rel }

Invoke-P4FileList -P4Args @('edit', '-c', $cl) -Paths @($toEdit | ForEach-Object { & $abs $_ })
foreach ($rel in (@($toEdit) + @($toAdd))) {
    $dest = & $abs $rel
    $dir = Split-Path $dest -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Copy-Item -LiteralPath $sourceFiles[$rel].Local -Destination $dest -Force
}
Invoke-P4FileList -P4Args @('add', '-c', $cl) -Paths @($toAdd | ForEach-Object { & $abs $_ })
Invoke-P4FileList -P4Args @('delete', '-c', $cl) -Paths @($toDelete | ForEach-Object { & $abs $_ })
$null = Invoke-P4 -P4Args (@('revert', '-a', '-c', $cl) + $targetSpecs)

$files = (Invoke-P4 -P4Args @('opened', '-c', $cl)).Records
Write-Host "CL $cl now holds $($files.Count) file(s):"
$files | ForEach-Object { Write-Host "  $($_.action.PadRight(7)) $($_.depotFile)" }

Write-Host @"

Next:
  1. Review CL $cl in P4V and submit it yourself.
  2. Then tag the commit:  .\scripts\sync_to_p4.ps1 -TagSubmitted <submitted CL number>
     (P4 may renumber the CL on submit, so use the number it reports.)
"@ -ForegroundColor Cyan
