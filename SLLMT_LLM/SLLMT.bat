@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto check

set "BASE_PY="
python -c "import sys; sys.exit(sys.version_info[:2] < (3, 14))" >nul 2>nul && set "BASE_PY=python"
if not defined BASE_PY py -3.14 -c "import sys" >nul 2>nul && set "BASE_PY=py -3.14"
if not defined BASE_PY (
  echo Python 3.14 or newer is required. Install it from https://www.python.org/downloads/
  echo During setup, tick "Add python.exe to PATH". Then run this file again.
  pause
  exit /b 1
)
echo First run: creating .venv in this folder and installing packages. This takes a few minutes.
%BASE_PY% -m venv .venv
if errorlevel 1 goto fail
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail

:check
"%VENV_PY%" -B run.py doctor >nul
if errorlevel 1 (
  "%VENV_PY%" -B run.py doctor
  echo Check failed. See "errors" above.
  pause
  exit /b 1
)
echo Starting SLLMT. Your browser opens at http://localhost:8501
echo Keep this window open while you use it. Close this window to stop.
"%VENV_PY%" -B run.py dashboard
pause
exit /b 0

:fail
echo Setup failed. Delete the .venv folder and run this file again.
pause
exit /b 1
