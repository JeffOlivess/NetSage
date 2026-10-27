@echo off
setlocal
title NetSage - F-VIT access point watch
cd /d "%~dp0"

rem ---- Access points (from your F-VIT scan). Edit these lines if needed. ----
set "SSID=F-VIT"
set "GOOD=30:cb:c7:90:08:60"
set "BAD1=a0:f8:49:85:fc:4e"
set "BAD2=70:b3:17:3e:37:0e"
set "INTERVAL=5"

net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo [1/4] Installing requirements (skipped if already installed)...
"%PY%" -c "import numpy" >nul 2>&1 || "%PY%" -m pip install -q -r requirements.txt
echo.
echo [2/4] Scanning %SSID% access points...
"%PY%" -m netsage.live.wifi_cli scan --ssid %SSID%
echo.
echo [3/4] Marking access points...
"%PY%" -m netsage.live.wifi_cli mark --bssid %GOOD% --as good
"%PY%" -m netsage.live.wifi_cli mark --bssid %BAD1% --as bad
"%PY%" -m netsage.live.wifi_cli mark --bssid %BAD2% --as bad
echo.
echo [4/4] Watching %SSID% every %INTERVAL% seconds. Press Ctrl+C to stop.
"%PY%" -m netsage.live.wifi_cli watch --ssid %SSID% --interval %INTERVAL% --loss-threshold 5
echo.
pause
