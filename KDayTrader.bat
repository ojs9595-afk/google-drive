@echo off
chcp 65001 >nul
title K-DayTrader
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist "launcher.py" (
  echo ZIP 압축을 먼저 푼 뒤, 풀린 폴더 안의 KDayTrader.bat 을 실행하세요.
  pause
  exit /b 1
)

set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.10 이상이 필요합니다. https://www.python.org/downloads/ 에서 설치하고,
  echo 설치 화면에서 "Add python.exe to PATH" 를 반드시 체크한 뒤 다시 실행하세요.
  pause
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul || (
    echo 오래된 가상환경을 정리합니다…
    rmdir /s /q .venv
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo 가상환경을 만드는 중… ^(최초 1회^)
  %PY% -m venv .venv || (
    echo 가상환경 생성에 실패했습니다. 전역 Python 으로 계속합니다.
    rmdir /s /q .venv 2>nul
  )
)
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
%PY% launcher.py %*
if errorlevel 1 pause
