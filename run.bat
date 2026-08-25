@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=py -3.11"

%PYTHON% -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 was not found. Install it with the Python launcher enabled.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local Python environment...
  %PYTHON% -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium

echo Starting Tensor.Art Batch Downloader...
python app.py
