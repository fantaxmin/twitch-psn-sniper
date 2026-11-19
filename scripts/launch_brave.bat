@echo off
if "%~1"=="--kick" goto kick
if "%~1"=="--twitch" goto twitch
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_brave.ps1" %*
exit /b %ERRORLEVEL%
:twitch
if "%~2"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_brave.ps1" -TwitchStream
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_brave.ps1" -TwitchStream -Channel "%~2"
)
exit /b %ERRORLEVEL%
:kick
if "%~2"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_brave.ps1" -KickStream
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_brave.ps1" -KickStream -Channel "%~2"
)
exit /b %ERRORLEVEL%
