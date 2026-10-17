# register_logon_task.ps1 — run once as Administrator
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "python not found" }

$taskName = "LocalServerLogonEnsure"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$here\logon_ensure.py`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force
Write-Host "Registered $taskName" -ForegroundColor Green
