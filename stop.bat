@echo off
rem Stop the installed Potbot runtime without downloading or syncing dependencies.
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
exit /b %ERRORLEVEL%
