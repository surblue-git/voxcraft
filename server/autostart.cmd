@echo off
rem VoxCraft ASR server launcher (used by autostart.vbs and for manual starts).
rem
rem ASCII only on purpose: cmd.exe reads .cmd files in the system codepage
rem (cp932 on Japanese Windows), so UTF-8 comments corrupt the parsing.
rem Japanese docs live in install-autostart.ps1 and README.md instead.
rem
rem Paths are relative to this file, so the folder can be moved.
rem Output goes to server.log, truncated on each start so it cannot grow forever.
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

echo [VoxCraft] starting %DATE% %TIME% on %VOXCRAFT_HOST%:%VOXCRAFT_PORT%> "%LOG%"
"%PY%" -m uvicorn main:app --host %VOXCRAFT_HOST% --port %VOXCRAFT_PORT% >> "%LOG%" 2>&1
