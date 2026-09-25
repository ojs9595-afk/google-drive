@echo off
setlocal EnableExtensions
chcp 65001 >nul
title K-DayTrader
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "launcher.py" goto nozip

rem ---- 1) 휴대용 Python 이 이미 준비되어 있으면 바로 실행 ----
if exist "runtime\python\.kdt-ready" goto runportable

rem ---- 2) 설치된 Python 3.10 이상이 있으면 그것으로 실행 ----
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul && set "PY=python"
if defined PY goto runsystem

rem ---- 3) Python 이 없으면 이 폴더 안에 휴대용 Python 을 자동 준비 (시스템 설치 아님) ----
:portablesetup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup_portable.ps1" -Root "%~dp0."
if errorlevel 1 goto setupfail

:runportable
"%~dp0runtime\python\python.exe" "%~dp0launcher.py" %*
if errorlevel 1 pause
exit /b

:runsystem
if not exist ".venv\Scripts\python.exe" goto mkvenv
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul
if not errorlevel 1 goto venvready
echo 오래된 가상환경을 정리합니다...
rmdir /s /q ".venv"
:mkvenv
echo 가상환경을 만드는 중... 최초 1회
%PY% -m venv .venv
if errorlevel 1 goto venvfail
:venvready
".venv\Scripts\python.exe" "%~dp0launcher.py" %*
if errorlevel 1 pause
exit /b

:venvfail
echo 가상환경 생성에 실패해 휴대용 Python 으로 전환합니다.
rmdir /s /q ".venv" 2>nul
goto portablesetup

:setupfail
echo.
echo 휴대용 Python 준비에 실패했습니다. 위 메시지를 확인하세요.
pause
exit /b 1

:nozip
echo ZIP 압축을 먼저 푼 뒤, 풀린 폴더 안의 KDayTrader.bat 을 실행하세요.
pause
exit /b 1
