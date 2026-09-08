@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0cairn-launcher.ps1" stop %*
if errorlevel 1 pause
