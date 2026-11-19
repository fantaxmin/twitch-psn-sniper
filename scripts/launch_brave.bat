@echo off
if "%~1"=="--kick" goto kick
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_brave.ps1" %*
exit /b %ERRORLEVEL%
:kick
if "%~2"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_brave.ps1" -KickStream
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_brave.ps1" -KickStream -Channel "%~2"
)
exit /b %ERRORLEVEL%
