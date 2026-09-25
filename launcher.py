#!/usr/bin/env python3
"""K-DayTrader 원클릭 런처: 의존성 확인/설치 → 설정 파일 준비 → 앱 서버 시작 → 브라우저 열기.

  python launcher.py            # 브라우저 자동 열기
  python launcher.py --no-browser --port 8787
"""
from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIRED = {"numpy": "numpy", "pandas": "pandas", "yaml": "PyYAML", "requests": "requests", "rich": "rich", "websockets": "websockets"}


def _print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # 콘솔 인코딩이 한글을 못 쓰는 경우
        print(msg.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)


def ensure_console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def missing_packages() -> list[str]:
    missing = []
    for mod, pkg in REQUIRED.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    return missing


def install_packages(pkgs: list[str]) -> bool:
    _print(f"필요한 패키지를 설치합니다: {', '.join(pkgs)} (1~2분 소요)")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(ROOT / "requirements.txt")]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT))
        if r.returncode != 0:
            _print("설치 실패. 관리자 권한 또는 --user 옵션으로 다시 시도합니다…")
            r = subprocess.run(cmd + ["--user"], cwd=str(ROOT))
        return r.returncode == 0 and not missing_packages()
    except Exception as e:
        _print(f"pip 실행 오류: {e}")
        return False


def main() -> int:
    ensure_console_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    os.chdir(ROOT)
    if sys.version_info < (3, 10):
        _print(f"Python 3.10 이상이 필요합니다 (현재 {sys.version.split()[0]}). https://www.python.org/downloads/ 에서 설치하세요.")
        return 2
    _print("=" * 56)
    _print("  K-DayTrader  한국주식 데이트레이딩 엔진")
    _print("=" * 56)
    pkgs = missing_packages()
    if pkgs and not install_packages(pkgs):
        _print("패키지 설치에 실패했습니다. 터미널에서 다음을 실행해 보세요:")
        _print(f"  {sys.executable} -m pip install -r requirements.txt")
        input("엔터를 누르면 종료합니다…")
        return 1

    cfg_path = ROOT / args.config
    if not cfg_path.exists() and (ROOT / "config.example.yaml").exists():
        cfg_path.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        _print(f"설정 파일을 만들었습니다: {cfg_path.name}")

    import logging

    from kdaytrader.app import AppController, AppServer, find_free_port

    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(ROOT / "logs" / f"app_{time.strftime('%Y%m%d')}.log", encoding="utf-8")],
    )
    port = find_free_port(args.host, args.port)
    ctl = AppController(str(cfg_path))
    server = AppServer(ctl, args.host, port)
    server.start()
    url = server.url
    _print(f"\n  ▶ 브라우저에서 열기:  {url}\n")
    _print("  이 창을 닫으면 프로그램이 종료됩니다. (Ctrl+C 로 종료)")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _print("종료 중…")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
