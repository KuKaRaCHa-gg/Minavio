@echo off
REM Double-clic : ouvre l interface Minavio dans le navigateur.
cd /d "%~dp0.."
uv run minavio ui
pause
