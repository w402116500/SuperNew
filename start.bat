@echo off
setlocal

cd /d "%~dp0"

where.exe pwsh.exe >nul 2>&1
if %errorlevel% equ 0 (
    pwsh.exe -NoLogo -NoProfile -File "%~dp0scripts\supermew.ps1" -Action start %*
) else (
    powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\supermew.ps1" -Action start %*
)

set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Project startup failed with exit code %EXIT_CODE%.
)

pause
exit /b %EXIT_CODE%
