#!/usr/bin/env bash
# macOS: Finder 에서 더블클릭으로 실행 (최초 1회 "열기" 허용 필요할 수 있음)
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "가상환경을 만드는 중… (최초 1회)"
  python3 -m venv .venv
fi
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
"$PY" launcher.py "$@"
