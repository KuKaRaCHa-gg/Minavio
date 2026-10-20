@echo off
REM Ingestion automatique : surveille un dossier de clips et lance le montage par lots.
REM Usage : minavio-watch.bat [profil] [dossier]
REM   Defaut : profil minivitch, dossier %USERPROFILE%\Videos\Outplayed.
REM Ne publie jamais sur YouTube.
REM
REM Demarrage avec Windows : Win+R, taper shell:startup, puis y deposer un raccourci
REM vers ce fichier (clic droit sur minavio-watch.bat > Creer un raccourci, puis
REM deplacer le raccourci dans le dossier qui s'ouvre).
setlocal
set "PROFILE=%~1"
if "%PROFILE%"=="" set "PROFILE=minivitch"
set "FOLDER=%~2"
if "%FOLDER%"=="" set "FOLDER=%USERPROFILE%\Videos\Outplayed"
cd /d "%~dp0.."
uv run minavio watch "%PROFILE%" "%FOLDER%"
pause
