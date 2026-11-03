@echo off
rem One-click launcher for enable-headless-unlock.ps1.
rem Why: avoids "running scripts is disabled" (ExecutionPolicy) and path mistakes.
rem The .ps1 raises its own UAC prompt when it needs admin.
rem
rem   enable-headless-unlock.cmd               install
rem   enable-headless-unlock.cmd -DryRun       show planned changes only
rem   enable-headless-unlock.cmd -Rollback     revert everything
setlocal
set "SCRIPT=%~dp0enable-headless-unlock.ps1"
if not exist "%SCRIPT%" (
    echo [ERROR] not found: "%SCRIPT%"
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "CODE=%ERRORLEVEL%"
echo.
echo [done] exit code = %CODE%
pause
exit /b %CODE%
