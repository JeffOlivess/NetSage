@echo off
setlocal EnableExtensions
title NetSage
cd /d "%~dp0"

rem ==========================================================================
rem  NetSage - one-click launcher
rem    1. asks for Administrator rights (needed for the live fault lab)
rem    2. creates a private Python environment (.venv) on first run
rem    3. installs the requirements (first run only)
rem    4. starts the server and opens the dashboard in your browser
rem  Close this window to stop NetSage. Any live fault is undone on exit.
rem ==========================================================================

set "PORT=5000"

net session >nul 2>&1
if not errorlevel 1 goto :admin_ok
if /i "%~1"=="--noadmin" goto :admin_skip
if /i "%~1"=="--elevated" goto :admin_skip
echo Asking for Administrator rights...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs -ErrorAction Stop; exit 0 } catch { exit 1 }"
if not errorlevel 1 exit /b 0
:admin_skip
echo.
echo [!] Running WITHOUT admin: simulation and live monitoring work, but the
echo     packet-loss / outage / DNS / adapter live faults are disabled.
echo.
goto :find_python
:admin_ok
echo Running as Administrator.

:find_python
set "PY="
py -3 -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [X] Python 3.9 or newer was not found.
  echo     Install it from https://www.python.org/downloads/ and tick
  echo     "Add python.exe to PATH", then run this file again.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment in .venv  ^(first run only^)...
  %PY% -m venv .venv
  if errorlevel 1 ( echo [X] Could not create .venv & pause & exit /b 1 )
)
set "VPY=%~dp0.venv\Scripts\python.exe"

"%VPY%" -c "import flask, numpy" >nul 2>&1
if errorlevel 1 (
  echo Installing requirements  ^(first run only, about a minute^)...
  "%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip
  "%VPY%" -m pip install --disable-pip-version-check -r requirements.txt
  "%VPY%" -c "import flask, numpy" >nul 2>&1
  if errorlevel 1 (
    echo [X] Installing the requirements failed. Check your internet connection,
    echo     delete the .venv folder and run this file again.
    pause
    exit /b 1
  )
)

powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%PORT%/api/state' | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  echo NetSage is already running - opening it.
  start "" "http://127.0.0.1:%PORT%/"
  timeout /t 3 >nul
  exit /b 0
)
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1 && set "PORT=5050"

start "" /min powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 120;$i++){ try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%PORT%/api/state' | Out-Null; Start-Process 'http://127.0.0.1:%PORT%/'; break } catch { Start-Sleep -Milliseconds 700 } }"

echo.
echo  ==============================================================
echo    NetSage is starting:  http://127.0.0.1:%PORT%/
echo    Keep this window open. Close it (or press Ctrl+C) to stop.
echo  ==============================================================
echo.
set "PYTHONUNBUFFERED=1"
"%VPY%" app.py
echo.
echo NetSage stopped.
pause
