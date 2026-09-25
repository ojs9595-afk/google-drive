"""두 번째 감사(앱 계층)에서 확인된 결함의 회귀 테스트."""
import json
import time
import urllib.request
from pathlib import Path

import pytest

from kdaytrader.config import DEFAULTS, load_config, validate_config, yaml_load


def test_yaml_unquoted_codes_are_not_octal(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("watchlist:\n  000660: SK하이닉스\n  005930: 삼성전자\n  '035420': NAVER\n", encoding="utf-8")
    cfg = load_config(str(cfg_path))
    assert set(cfg["watchlist"]) == {"000660", "005930", "035420"}
    assert yaml_load("a: 010") == {"a": "010"} and yaml_load("a: 0x10") == {"a": 16} and yaml_load("a: 12") == {"a": 12} and yaml_load("a: 1.5") == {"a": 1.5}


def test_defaults_not_mutated_and_null_sections(tmp_path: Path):
    before = json.dumps(DEFAULTS, sort_keys=True, default=str)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("watchlist:\nkis:\n  paper: false\nlog_dir:\n", encoding="utf-8")
    cfg = load_config(str(cfg_path))
    cfg["kis"]["app_key"] = "X"
    cfg["screener"]["limit"] = 1
    assert json.dumps(DEFAULTS, sort_keys=True, default=str) == before
    assert cfg["watchlist"] == dict(DEFAULTS["watchlist"]) and cfg["log_dir"] == "logs" and cfg["kis"]["paper"] is False


def test_broken_yaml_falls_back_with_error(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("risk: [\n", encoding="utf-8")
    cfg = load_config(str(cfg_path))
    assert cfg["_config_error"] and cfg["watchlist"]


def test_validate_config_rejects_bad_values():
    with pytest.raises(ValueError):
        validate_config({**DEFAULTS, "interval_min": 0})
    with pytest.raises(ValueError):
        validate_config({**DEFAULTS, "initial_cash": 0})
    validate_config(dict(DEFAULTS))


def test_env_placeholders_preserved_on_save(tmp_path: Path, monkeypatch):
    from kdaytrader.app import AppController

    monkeypatch.setenv("KIS_APP_KEY", "envkey")
    monkeypatch.setenv("KIS_APP_SECRET", "envsecret")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("kis:\n  app_key: ${KIS_APP_KEY:-}\n  app_secret: ${KIS_APP_SECRET:-}\n  account: ${KIS_ACCOUNT:-}\nwatchlist:\n  '005930': 삼성전자\n", encoding="utf-8")
    ctl = AppController(str(cfg_path))
    assert ctl.status()["kis_configured"] is True
    shown = ctl.config_get()
    assert shown["kis"]["app_key"] == "" and "kis.app_key" in shown["_env"]
    shown["risk"] = {"risk_per_trade": 0.02}
    ctl.config_save(shown)
    text = cfg_path.read_text(encoding="utf-8")
    assert "${KIS_APP_KEY:-}" in text and ctl.status()["kis_configured"] is True
    # 사용자가 실제 값을 입력하면 그 값으로 대체
    shown["kis"]["app_key"] = "typed"
    ctl.config_save(shown)
    assert "typed" in cfg_path.read_text(encoding="utf-8")


def test_sim_trades_kept_separate_from_real_performance(tmp_path: Path):
    from kdaytrader.performance import CSV_HEADER, load_trade_files

    (tmp_path / "sim_trades_20260901.csv").write_text(",".join(CSV_HEADER) + "\n2026-09-01 09:30:00,2026-09-01 09:40:00,005930,삼성전자,10,70000,71000,9000,1.4,100,익절,sim/paper\n", encoding="utf-8-sig")
    (tmp_path / "trades_20260901.csv").write_text(",".join(CSV_HEADER) + "\n2026-09-01 10:30:00,2026-09-01 10:40:00,000660,SK하이닉스,1,200000,201000,900,0.5,50,익절,naver/paper\n2026-09-01 11:00:00,2026-09-01 11:05:00,000660\n", encoding="cp949")
    real = load_trade_files(tmp_path)
    assert [t["code"] for t in real] == ["000660"] and real[0]["mode"] == "naver/paper"  # 잘린 행·CP949 파일도 안전
    both = load_trade_files(tmp_path, include_sim=True)
    assert len(both) == 2


def test_performance_zero_initial_cash():
    from datetime import datetime

    from kdaytrader.market import KST
    from kdaytrader.performance import performance_report

    t = {"entry_ts": datetime(2026, 9, 1, 9, tzinfo=KST), "exit_ts": datetime(2026, 9, 1, 9, 30, tzinfo=KST), "code": "A", "name": "A", "qty": 1, "entry_price": 1.0, "exit_price": 2.0, "pnl": 1.0, "pnl_pct": 100.0, "fees": 0.0, "reason": "익절", "source": "s"}
    rep = performance_report([t], days=None, initial_cash=0)
    assert rep["daily"][0]["ret_pct"] == 0.0 and rep["kpi"]["return_pct"] == 0


def test_screener_fund_filter():
    from kdaytrader.screener import is_fund_like

    assert is_fund_like("KODEX 200") and is_fund_like("TIGER 미국나스닥100") and is_fund_like("삼성 레버리지 WTI원유 선물 ETN")
    assert not is_fund_like("삼성전자") and not is_fund_like("SK하이닉스")


def test_stop_during_warmup_prevents_start(tmp_path: Path):
    from kdaytrader.app import AppController, AppServer, find_free_port

    cfg = tmp_path / "config.yaml"
    cfg.write_text("watchlist:\n  '005930': 삼성전자\nsim:\n  speed: 0\n  history_days: 1\nlog_dir: " + str(tmp_path) + "\n", encoding="utf-8")
    ctl = AppController(str(cfg))
    ctl.start(feed="sim", mode="paper", use_news=False, sim_speed=0)
    assert ctl.stop(timeout=15)
    assert not ctl.engine_running
    # 정지 후 재시작 가능
    ctl.start(feed="sim", mode="paper", use_news=False, sim_speed=0)
    assert ctl.stop(timeout=15) and not ctl.engine_running
    assert not list(tmp_path.glob("trades_*.csv"))  # 시뮬레이션 거래는 실거래 파일에 기록되지 않음
