#!/usr/bin/env bash
# Linux: 터미널에서 ./KDayTrader.sh 또는 파일 관리자에서 실행
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "가상환경을 만드는 중… (최초 1회)"
  python3 -m venv .venv
fi
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
"$PY" launcher.py "$@"
