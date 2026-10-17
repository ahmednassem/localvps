# uninstall.ps1 — stop and remove the Local VPS Core service.
# Run from an elevated PowerShell.

$ErrorActionPreference = "SilentlyContinue"
$here = $PSScriptRoot
Set-Location $here

$py = (Get-Command python).Source

Write-Host "Stopping service..." -ForegroundColor Cyan
& $py "$here\service.py" stop
Start-Sleep -Seconds 2

Write-Host "Removing service..." -ForegroundColor Cyan
& $py "$here\service.py" remove

sc.exe query LocalServerCore
Write-Host "Removed (if it still shows, reboot to clear the SCM entry)." -ForegroundColor Green
