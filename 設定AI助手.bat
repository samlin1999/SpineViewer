@echo off
setlocal
chcp 65001 >nul
title spineview - setup AI agent
cd /d "%~dp0"

rem ASCII only. All Chinese messages come from Python, because cmd.exe loses
rem sync while parsing a UTF-8 batch file that contains non-ASCII text.

set "PY="
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto :nopython

rem Checks the installed distribution, not "import spineview" (that always
rem succeeds from this folder). Installing with -e registers the "spineview" command that AI agents call.
%PY% -c "import PIL, numpy, importlib.metadata as m; m.distribution('spineview')" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   First run: installing spineview and its dependencies...
    echo.
    %PY% -m pip install -e .
    if errorlevel 1 goto :nodeps
)

rem %* keeps every dragged folder, so several projects can be set up at once.
%PY% spineview.py setup %*
if errorlevel 1 pause
goto :end

:nopython
echo.
echo   Python not found.
echo.
echo   Please install Python 3.9 or newer:  https://www.python.org/downloads/
echo   Remember to tick "Add Python to PATH" during the installation.
echo.
pause
goto :end

:nodeps
echo.
echo   Installation failed. Please run this manually:
echo       pip install -e .
echo.
pause
goto :end

:end
endlocal
