@echo off
chcp 65001 >nul
title K-DayTrader
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

where py >nul 2>nul && (set "PY=py -3") || (where python >nul 2>nul && (set "PY=python") || (
  echo Python 3.10 이상이 필요합니다. https://www.python.org/downloads/ 에서 설치 시 "Add python.exe to PATH" 를 체크하세요.
  pause
  exit /b 1
))

if not exist ".venv\Scripts\python.exe" (
  echo 가상환경을 만드는 중… ^(최초 1회^)
  %PY% -m venv .venv
)
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
)
%PY% launcher.py %*
if errorlevel 1 pause
