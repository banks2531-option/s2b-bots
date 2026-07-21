<#
    sync_reports.ps1 — pull the droplet's standardized review files into this repo (ONE-WAY).

    The droplet writes reports/latest/ at 10:00, 12:00, 14:00, 15:45, 16:15 ET. This script copies
    them down so a locally-running Codex reviewer can read them. It is deliberately PULL-ONLY: there
    is no path from this machine back to production, so an automated reviewer can never affect the
    live bot.

    Schedule it in Windows Task Scheduler a few minutes after each generation slot (e.g. :06 past
    the hour at 10, 12, 14, 15:51, 16:21 ET), on days the machine is awake. If a run is missed, the
    next one simply pulls the current files — nothing is lost, the droplet copy is always current.

    Run manually any time:  powershell -ExecutionPolicy Bypass -File reports\sync_reports.ps1
#>

$ErrorActionPreference = "Stop"
$Key   = "C:\Users\FixUser123\RECOVERY\.ssh\id_ed25519"
$Host_ = "root@159.89.45.162"
$Src   = "/root/s2b-bot/reports/latest/*"
$Repo  = Split-Path -Parent $PSScriptRoot           # repo root = parent of reports\
$Dest  = Join-Path $Repo "reports\latest"

if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Force -Path $Dest | Out-Null }

# Use Git's bundled scp, NOT Windows System32 OpenSSH. The Git client already trusts this host
# (known_hosts at ~\.ssh) and authenticates cleanly; the System32 client closes the connection
# under BatchMode when the host key isn't in ITS store. Fall back to plain "scp" if Git isn't found.
$Scp = "C:\Program Files\Git\usr\bin\scp.exe"
if (-not (Test-Path $Scp)) { $Scp = "scp" }

& $Scp -q -o BatchMode=yes -o ConnectTimeout=20 -i $Key "${Host_}:$Src" $Dest
if ($LASTEXITCODE -ne 0) { Write-Error "scp failed (exit $LASTEXITCODE)"; exit 1 }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$manifest = Join-Path $Dest "manifest.json"
$slot = "?"
if (Test-Path $manifest) {
    try { $slot = (Get-Content $manifest -Raw | ConvertFrom-Json).review_slot } catch {}
}
Write-Output "$stamp  synced reports/latest/ (slot: $slot)"

# Optional: uncomment to auto-commit the synced snapshot so Codex can `git diff` between runs.
# & git -C $Repo add reports/latest 2>$null
# & git -C $Repo commit -q -m "reports: sync $slot ($stamp)" 2>$null
