@echo off
chcp 65001 >nul
REM K-DayTrader CLI 메뉴 (Windows). 그래픽 앱은 KDayTrader.bat
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.10 이상을 설치하고 "Add python.exe to PATH" 를 체크하세요. https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || (echo 가상환경 생성 실패 & pause & exit /b 1)
)
set "PY=.venv\Scripts\python.exe"
%PY% -m pip install -q --disable-pip-version-check -r requirements.txt
if not exist config.yaml copy config.example.yaml config.yaml >nul
%PY% main.py %*
pause
