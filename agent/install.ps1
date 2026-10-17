# install.ps1 — register the Local VPS Core as a native Windows service.
# Run from an elevated PowerShell (Run as Administrator).
#
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
#   .\install.ps1

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
Set-Location $here

# Use the current Python (must have pywin32 + deps). Prefer pythonw for the
# service host so no console flashes; fall back to python.
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "python not found on PATH" }

Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $py -m pip install -r "$here\requirements.txt"

# pywin32 post-install (registers pythonservice.exe support), harmless if re-run
& $py -m pywin32_postinstall -install 2>$null | Out-Null

Write-Host "Registering service LocalServerCore..." -ForegroundColor Cyan
& $py "$here\service.py" install --startup auto

# Run as LocalSystem (default) and auto-restart on failure via the SCM.
sc.exe config LocalServerCore start= auto | Out-Null
sc.exe failure LocalServerCore reset= 0 actions= restart/5000/restart/5000/restart/5000 | Out-Null

Write-Host "Starting service..." -ForegroundColor Cyan
& $py "$here\service.py" start

Start-Sleep -Seconds 2
sc.exe query LocalServerCore

# At every user logon, ensure the core service is running (covers manual laptop open).
Write-Host "Registering logon ensure task..." -ForegroundColor Cyan
$taskName = "LocalServerLogonEnsure"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$here\logon_ensure.py`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null

Write-Host ""
Write-Host "Done. Reminders:" -ForegroundColor Green
Write-Host " - Fill in .env (copy from .env.example) before it can connect."
Write-Host " - Drop LibreHardwareMonitorLib.dll next to hwtemp.py for CPU temps."
Write-Host " - Set the HP BIOS to power on when AC is applied (for the plug wake)."
