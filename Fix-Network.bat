@echo off
setlocal EnableExtensions
title NetSage - restore network
cd /d "%~dp0"
rem Emergency undo: removes every NetSage live fault and turns Wi-Fi back on.
rem Only needed if the NetSage window was killed in the middle of a live fault.

net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)

echo Removing NetSage firewall rules...
for %%R in (Loss Outage DNS-UDP DNS-TCP) do netsh advfirewall firewall delete rule name="NetSage-Fault-%%R" >nul 2>&1

echo Re-enabling any disabled Wi-Fi adapter...
powershell -NoProfile -Command "Get-NetAdapter | Where-Object { $_.Status -eq 'Disabled' -and ($_.Name -match 'Wi-?Fi|WLAN|Wireless' -or $_.InterfaceDescription -match 'Wi-?Fi|Wireless|802\.11|WLAN') } | Enable-NetAdapter -Confirm:$false"

echo Flushing DNS cache...
ipconfig /flushdns >nul
if exist "data\live_fault_state.json" del /q "data\live_fault_state.json"

echo.
echo Done. If Wi-Fi does not reconnect by itself, click your network in the taskbar.
pause
