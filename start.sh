#!/usr/bin/env bash
# K-DayTrader 실행 스크립트 (macOS/Linux). 가상환경 생성 → 의존성 설치 → 메뉴 실행
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt
[ -f config.yaml ] || cp config.example.yaml config.yaml
python main.py "$@"
