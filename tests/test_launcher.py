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
