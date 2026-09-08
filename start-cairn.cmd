@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0cairn-launcher.ps1" start %*
if errorlevel 1 pause
