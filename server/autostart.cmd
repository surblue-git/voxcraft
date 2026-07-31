@echo off
rem VoxCraft ASR server launcher (used by autostart.vbs and for manual starts).
rem
rem ASCII only on purpose: cmd.exe reads .cmd files in the system codepage
rem (cp932 on Japanese Windows), so UTF-8 comments corrupt the parsing.
rem Japanese docs live in install-autostart.ps1 and README.md instead.
rem
rem Paths are relative to this file, so the folder can be moved.
rem Output goes to server.log. The previous five runs are kept as server.log.1
rem through server.log.5 (see the rotation below).
rem Settings come from environment variables (see config.py); use setx to make
rem them apply at logon, e.g.  setx VOXCRAFT_MODEL v3-turbo

setlocal
cd /d "%~dp0"

if "%VOXCRAFT_HOST%"=="" set "VOXCRAFT_HOST=0.0.0.0"
if "%VOXCRAFT_PORT%"=="" set "VOXCRAFT_PORT=8760"

set "LOG=%~dp0server.log"

rem Do nothing if the port is already served, so we never fight over it.
rem Note: do not touch the log here, the running server holds it open.
netstat -ano | findstr /c:":%VOXCRAFT_PORT% " | findstr /i "LISTENING" >nul
if not errorlevel 1 (
    echo [VoxCraft] already running on port %VOXCRAFT_PORT%, nothing to do.
    exit /b 0
)

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem Force UTF-8 for stdout/stderr. Without this, Python encodes log lines in the
rem system codepage (cp932) and the Japanese diagnostics in server.log become
rem unreadable mojibake, which defeats the purpose of logging them.
set "PYTHONIOENCODING=utf-8"

rem Keep the previous five runs instead of truncating. The diagnostics written
rem here (empty-chunk positions, recognition failures) can only be read after
rem the fact, and restarting the server used to destroy them first. Each file
rem starts with its own "[VoxCraft] starting DATE TIME" line, so the newest
rem interesting run can be found by looking at the first line of each.
rem move /y overwrites, so the oldest generation falls off on its own.
rem This runs only past the "already running" check above: the live server
rem holds server.log open and moving it out from under it would break logging.
if exist "%LOG%.4" move /y "%LOG%.4" "%LOG%.5" >nul
if exist "%LOG%.3" move /y "%LOG%.3" "%LOG%.4" >nul
if exist "%LOG%.2" move /y "%LOG%.2" "%LOG%.3" >nul
if exist "%LOG%.1" move /y "%LOG%.1" "%LOG%.2" >nul
if exist "%LOG%" move /y "%LOG%" "%LOG%.1" >nul

echo [VoxCraft] starting %DATE% %TIME% on %VOXCRAFT_HOST%:%VOXCRAFT_PORT%> "%LOG%"
"%PY%" -m uvicorn main:app --host %VOXCRAFT_HOST% --port %VOXCRAFT_PORT% >> "%LOG%" 2>&1
