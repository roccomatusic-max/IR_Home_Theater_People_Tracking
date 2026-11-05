# Removes the "Theater Walkway" startup task and its firewall rules. Run as Administrator.
$ErrorActionPreference = "Stop"
$TaskName = "Theater Walkway"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task '$TaskName'."
}
Get-NetFirewallRule -DisplayName "Theater Walkway*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "Removed firewall rules. The repo folder, .venv and config.yaml were left in place."
