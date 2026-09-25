#!/usr/bin/env bash
# macOS: Finder 에서 더블클릭 (처음엔 우클릭 → 열기). Python 3.10+ 필요
cd "$(dirname "$0")"
if [ ! -f launcher.py ]; then
  echo "ZIP 압축을 먼저 푼 뒤, 풀린 폴더 안에서 실행하세요."
  read -r -p "엔터를 누르면 종료합니다" _; exit 1
fi
# 3.10 이상인 파이썬 찾기
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 이상이 필요합니다. https://www.python.org/downloads/ 에서 설치 후 다시 실행하세요."
  read -r -p "엔터를 누르면 종료합니다" _; exit 1
fi
# 오래된 가상환경(3.10 미만) 정리
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null; then
  echo "오래된 가상환경을 정리합니다…"; rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  echo "가상환경을 만드는 중… (최초 1회)"
  if ! "$PY" -m venv .venv || ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    rm -rf .venv
    echo "가상환경 생성에 실패했습니다 (Ubuntu/Debian: sudo apt install python3-venv). 전역 Python 으로 계속합니다."
  fi
fi
[ -x .venv/bin/python ] && PY=.venv/bin/python
"$PY" launcher.py "$@"
