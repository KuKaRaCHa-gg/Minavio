@echo off
REM Double-clic : installation Windows en un clic (PowerShell + winget + uv + npm).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
