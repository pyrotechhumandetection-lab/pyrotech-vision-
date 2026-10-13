# PYROTECH VISION - find (and optionally stop) programs that have files of backend\.venv open.
# Windows cannot replace a .pyd/.dll that a running program has loaded (pip then fails with
# "[WinError 5] Access is denied ... cv2.pyd"). Usually this is an old backend window still running.
#
#   venv-in-use.ps1 -Venv backend\.venv          exit 0 = nothing uses it, exit 3 = list printed
#   venv-in-use.ps1 -Venv backend\.venv -Stop    stops those programs, exit 0 when all stopped
param([Parameter(Mandatory = $true)][string]$Venv, [switch]$Stop)

$ErrorActionPreference = 'SilentlyContinue'
if (-not (Test-Path -LiteralPath $Venv)) { exit 0 }
$root = (Resolve-Path -LiteralPath $Venv).Path.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar

function Find-Users {
    $hits = @()
    foreach ($p in Get-Process) {
        if ($p.Id -eq $PID) { continue }
        $paths = @()
        if ($p.Path) { $paths += $p.Path }
        try { foreach ($m in $p.Modules) { if ($m.FileName) { $paths += $m.FileName } } } catch { }
        foreach ($f in $paths) {
            if ($f.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { $hits += $p; break }
        }
    }
    return $hits
}

$users = @(Find-Users)
if ($users.Count -eq 0) { exit 0 }

if (-not $Stop) {
    Write-Host ''
    Write-Host ' These programs are using backend\.venv:'
    foreach ($p in $users) {
        $cmd = $null
        try { $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)").CommandLine } catch { }
        if (-not $cmd) { $cmd = $p.Path }
        Write-Host ('   PID {0,-7} {1}' -f $p.Id, $cmd)
    }
    exit 3
}

foreach ($p in $users) {
    try { Stop-Process -Id $p.Id -Force -ErrorAction Stop; Write-Host " Stopped PID $($p.Id)." }
    catch { Write-Host " Could not stop PID $($p.Id): $($_.Exception.Message)" }
}
Start-Sleep -Seconds 2
$left = @(Find-Users)
if ($left.Count -gt 0) {
    Write-Host ' Still running (close them yourself, e.g. in Task Manager):' ($left | ForEach-Object { $_.Id })
    exit 1
}
exit 0
