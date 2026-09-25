@echo off
REM K-DayTrader 실행 스크립트 (Windows). 가상환경 생성 → 의존성 설치 → 메뉴 실행
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if not exist config.yaml copy config.example.yaml config.yaml >nul
python main.py %*
pause
