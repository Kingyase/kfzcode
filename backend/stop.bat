@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  KFZCode Backend -- Stop Script
REM  Strategy: 1) run.py stop (wmic scan)  2) verify + netstat kill
REM ============================================================

echo ========================================
echo   KFZCode Backend -- Stopping...
echo ========================================
echo.

REM Step 1: Python-based stop (wmic scan is most reliable)
echo [Step 1] run.py stop (wmic scan)...

python "%~dp0run.py" stop 2>&1
if %ERRORLEVEL% EQU 0 goto :verify

py "%~dp0run.py" stop 2>&1
if %ERRORLEVEL% EQU 0 goto :verify

python3 "%~dp0run.py" stop 2>&1
if %ERRORLEVEL% EQU 0 goto :verify

echo [WARN] Python not available, falling back to netstat...

REM Step 2: Port-based kill (fallback if Python unavailable)
:portkill
echo [Step 2] Killing processes on port 8765...
set "FOUND="
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8765" ^| findstr "LISTENING"') do (
    set "FOUND=1"
    echo Stopping PID %%a on port 8765...
    taskkill /F /T /PID %%a 2>&1
)

if not defined FOUND (
    echo No process found listening on port 8765.
    goto :cleanup
)

REM Wait and re-check
ping -n 4 127.0.0.1 >nul
goto :verify

REM Step 3: Verify port is actually clear
:verify
echo [Verify] Checking port 8765...
netstat -ano 2>nul | findstr ":8765" | findstr "LISTENING" >nul
if %ERRORLEVEL% EQU 0 (
    echo [WARN] Port 8765 still occupied! Retrying with netstat...
    goto :portkill
)
echo Port 8765 is clear.

REM Step 4: Clean up cmd window with KFZCode-Backend title
:cleanup
for /f "tokens=2" %%a in ('tasklist /v /fi "WINDOWTITLE eq KFZCode-Backend" /fo list 2^>nul ^| findstr "PID:"') do (
    echo Stopping cmd window PID %%a...
    taskkill /F /T /PID %%a >nul 2>&1
)

echo.
echo Done.
ping -n 4 127.0.0.1 >nul
endlocal
