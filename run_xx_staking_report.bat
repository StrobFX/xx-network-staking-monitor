@echo off
setlocal

set "SCRIPT_PATH=%~dp0xx_staking_report.py"

if not exist "%SCRIPT_PATH%" (
    echo Script not found:
    echo %SCRIPT_PATH%
    pause
    exit /b 1
)

where py.exe >nul 2>nul
if not errorlevel 1 (
    py.exe -3 "%SCRIPT_PATH%" %*
) else (
    python.exe "%SCRIPT_PATH%" %*
)
set "REPORT_EXIT=%ERRORLEVEL%"

echo.
if not "%REPORT_EXIT%"=="0" (
    echo Report stopped with error code %REPORT_EXIT%.
) else (
    echo Report finished. Files are in xx_staking_output.
)
pause
exit /b %REPORT_EXIT%
