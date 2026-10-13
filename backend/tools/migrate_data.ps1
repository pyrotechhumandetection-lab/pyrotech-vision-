<#
  OPTIONAL one-time import: copy cameras, credentials, alerts and snapshot images that an earlier
  installation of the backend saved elsewhere into this repository. The backend never reads the
  source folder at runtime - after the copy, this repository is self-contained.

  Copies (never moves or deletes):
    <old>\data\cameras.json, credentials.json, alerts.json, detections.json, snapshots.json  ->  backend\data\
    <old>\data\snapshots\*                                                                  ->  backend\snapshots\

  credentials.json is encrypted with Windows DPAPI for your Windows user. It keeps working as long as
  the new backend runs under the same Windows account.

  Usage (from the repository root, with the OLD backend stopped). The source folder is the one that
  contains the old "data" folder, e.g. the original "human detection" folder:
    powershell -ExecutionPolicy Bypass -File backend\tools\migrate_data.ps1 -From "<old folder>"
#>
param(
    [Parameter(Mandatory = $true)][string]$From,
    [switch]$Force
)
$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$srcData = Join-Path $From "data"
$dstData = Join-Path $backend "data"
$dstSnap = Join-Path $backend "snapshots"

if (-not (Test-Path (Join-Path $srcData "cameras.json"))) { Write-Error "No backend data found in '$srcData'."; exit 1 }

# The old backend must not be writing while we copy.
$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listening -and -not $Force) {
    Write-Error "Something is listening on port 8000 (the old backend?). Stop it first (Ctrl+C in its window), or re-run with -Force."
    exit 1
}
if ((Test-Path (Join-Path $dstData "cameras.json")) -and -not $Force) {
    Write-Error "backend\data\cameras.json already exists - not overwriting. Re-run with -Force to replace it."
    exit 1
}

New-Item -ItemType Directory -Force -Path $dstData, $dstSnap | Out-Null
foreach ($f in "cameras.json", "credentials.json", "alerts.json", "detections.json", "snapshots.json") {
    $p = Join-Path $srcData $f
    if (Test-Path $p) { Copy-Item $p -Destination $dstData -Force; Write-Host "  copied data\$f" }
}
$srcSnap = Join-Path $srcData "snapshots"
if (Test-Path $srcSnap) {
    Copy-Item (Join-Path $srcSnap "*") -Destination $dstSnap -Recurse -Force
}

# Verify: every snapshot record must point to an existing image under backend\snapshots
$records = Get-Content (Join-Path $dstData "snapshots.json") -Raw | ConvertFrom-Json
$missing = @($records | Where-Object { -not (Test-Path (Join-Path $dstSnap ($_.path -replace "/", "\"))) })
$images = @(Get-ChildItem $dstSnap -Recurse -File -Filter *.jpg).Count
Write-Host ""
Write-Host "Snapshot records: $($records.Count)   images in backend\snapshots: $images   records without image: $($missing.Count)"
if ($missing.Count -gt 0) { Write-Warning "Some snapshot records have no image file (they were missing in the source too, or copy failed)." }
Write-Host "Done. Start the backend with start-backend.cmd and check Cameras / Snapshots / Alerts in the UI."
