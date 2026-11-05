@echo off
rem Double-click friendly wrapper: runs zs-dev.ps1 without needing to change the PowerShell execution policy.
rem   zs-dev setup | start [-Open] | stop | restart | status | logs [-Follow] | doctor | reset-password | reset-session | test | clean
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0zs-dev.ps1" %*
