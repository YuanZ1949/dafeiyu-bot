@echo off
setlocal
rem Double-click launcher for the supervisor (hidden window, managed children).
rem The dashboard page is opened by supervisor itself when dashboard.open_browser is true,
rem so this script passes -NoBrowser to avoid opening a second tab.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" -NoBrowser
if errorlevel 1 (
  echo.
  echo Startup failed. See logs\supervisor\supervisor.log
  pause
)
endlocal
