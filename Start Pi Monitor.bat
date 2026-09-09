@echo off
REM Double-click THIS file instead of the .py file.
REM The window stays open afterwards so any error can be read.

cd /d "%~dp0"
title Pi Horizon Monitor

echo Starting the Pi Horizon Monitor...
echo.

python pi_horizon_monitor.py
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% NEQ 0 (
    echo.
    echo ------------------------------------------------------------
    echo The monitor stopped with an error ^(code %EXITCODE%^).
    echo Any details are shown above and saved to pi_monitor_error.log
    echo.
    echo If it says PyQt6 or requests is missing, run this once:
    echo     python -m pip install PyQt6 requests
    echo ------------------------------------------------------------
)

echo.
pause
