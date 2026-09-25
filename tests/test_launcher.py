import socket
import subprocess
import sys
from pathlib import Path

import launcher
from kdaytrader.app import find_free_port


def test_dependencies_present():
    assert launcher.missing_packages() == []


def test_find_free_port_skips_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        port = find_free_port("127.0.0.1", busy)
        assert port != busy and busy < port <= busy + 20


def test_launcher_help_runs():
    r = subprocess.run([sys.executable, "launcher.py", "--help"], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "--no-browser" in r.stdout


def test_one_click_scripts_exist_and_reference_launcher():
    root = Path(__file__).resolve().parents[1]
    for name in ("KDayTrader.bat", "KDayTrader.command", "KDayTrader.sh"):
        text = (root / name).read_text(encoding="utf-8")
        assert "launcher.py" in text
    assert (root / "config.example.yaml").exists() and (root / "requirements.txt").exists()


def test_windows_scripts_encoding_and_line_endings():
    root = Path(__file__).resolve().parents[1]
    bat = (root / "KDayTrader.bat").read_bytes()
    assert b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")  # CRLF 만 (LF 전용이면 cmd 라벨 탐색 오류)
    assert not bat.startswith(b"\xef\xbb\xbf")  # BOM 이 있으면 첫 줄 @echo off 가 깨짐
    assert b"setup_portable.ps1" in bat and b"runtime\\python" in bat
    ps1 = (root / "tools" / "setup_portable.ps1").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf")  # PowerShell 5 가 한글을 UTF-8 로 읽도록 BOM 필요
    assert b"embed" in ps1 and b"get-pip.py" in ps1 and b"import site" in ps1


def test_market_timezone_fallback_is_kst():
    from datetime import timedelta
    from kdaytrader.market import KST, now_kst

    assert now_kst().utcoffset() == timedelta(hours=9) and KST is not None
