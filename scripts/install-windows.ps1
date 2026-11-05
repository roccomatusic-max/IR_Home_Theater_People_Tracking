<#
.SYNOPSIS
  Installs the theater walkway tracker so it starts with Windows and restarts if it stops.

.DESCRIPTION
  Run from an Administrator PowerShell in the repo folder:

      powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1

  It creates .venv, installs requirements, registers a "Theater Walkway" scheduled
  task that runs at startup as SYSTEM (restarting every minute if it exits), opens
  the firewall for the zone setup page and the PoE camera, and starts it.
  Logs go to logs\walkway.log. Re-run it any time to update; it replaces the task.

.PARAMETER Config
  Path to your config.yaml (default: config.yaml in the repo folder).

.PARAMETER NoSetupPage
  Don't serve the zone setup page on port 8099.
#>
param(
    [string]$Config = "config.yaml",
    [switch]$NoSetupPage,
    [int]$Port = 8099
)
$ErrorActionPreference = "Stop"
$TaskName = "Theater Walkway"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Please run this from an Administrator PowerShell." }

if (-not (Test-Path $Config)) {
    Copy-Item "config.example.yaml" $Config
    Write-Host "Created $Config from the example. Edit the mqtt section, then run this script again." -ForegroundColor Yellow
    exit 1
}
$ConfigPath = (Resolve-Path $Config).Path

# Python virtual environment
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Creating Python environment..."
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv .venv }
    else { throw "Python 3.10+ is not installed. Install it from python.org (tick 'Add to PATH') and re-run." }
}
Write-Host "Installing requirements..."
& $Python -m pip install --quiet --upgrade pip
& $Python -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null
$LogPath = Join-Path $Root "logs\walkway.log"
$ArgList = "-m walkway `"$ConfigPath`" --log `"$LogPath`""
if (-not $NoSetupPage) { $ArgList += " --setup --port $Port" }

# Scheduled task: at startup, as SYSTEM, restart every minute if it stops
$action = New-ScheduledTaskAction -Execute $Python -Argument $ArgList -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Lights the theater walkway someone is leaving by." -Force | Out-Null

# Firewall: the camera talks to Python over the network; the setup page listens on $Port
Get-NetFirewallRule -DisplayName "Theater Walkway*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "Theater Walkway (camera)" -Direction Inbound -Program $Python `
    -Action Allow -Profile Domain,Private | Out-Null
if (-not $NoSetupPage) {
    New-NetFirewallRule -DisplayName "Theater Walkway (setup page)" -Direction Inbound -Protocol TCP `
        -LocalPort $Port -Action Allow -Profile Domain,Private | Out-Null
}

Start-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "Installed and started '$TaskName'." -ForegroundColor Green
Write-Host "  Log:        $LogPath"
if (-not $NoSetupPage) { Write-Host "  Setup page: http://$($env:COMPUTERNAME):$Port" }
Write-Host "  Remove it:  scripts\uninstall-windows.ps1"
