@echo off
REM ============================================================
REM ALCOSOFT FRESH RESET - BATCH FILE
REM Simply double-click this file to reset everything
REM ============================================================

REM Change to the script directory
cd /d "%~dp0"

REM Run the Python reset script
python reset_system.py

REM Pause so you can see the output
pause
