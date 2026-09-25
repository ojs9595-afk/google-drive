#!/usr/bin/env bash
# K-DayTrader CLI 메뉴 (macOS/Linux). 그래픽 앱은 KDayTrader.command / KDayTrader.sh
set -e
cd "$(dirname "$0")"
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "Python 3.10 이상이 필요합니다."; exit 1; }
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv || { rm -rf .venv; echo "가상환경 생성 실패 (Ubuntu/Debian: sudo apt install python3-venv)"; exit 1; }
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
[ -f config.yaml ] || cp config.example.yaml config.yaml
.venv/bin/python main.py "$@"
