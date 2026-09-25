"""장전 자동 종목 선정 테스트 (시뮬레이션 데이터, 네트워크 없음)."""
import json
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from kdaytrader.app import AppController, AppServer, find_free_port
from kdaytrader.config import DEFAULTS, build_premarket_params
from kdaytrader.market import KST
from kdaytrader.premarket import PremarketParams, UniverseSelector, daily_metrics, next_run_text, score_stock, should_run_premarket


def _daily(n=60, price=50_000, vol=1_000_000, trend=0.002, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-07-01", periods=n, freq="B", tz=KST)
    c = price * np.exp(np.cumsum(rng.normal(trend, 0.015, n)))
    o = c * (1 + rng.normal(0, 0.004, n))
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.01, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.01, n)))
    v = np.full(n, vol, dtype=float)
    v[-1] = vol * 2.5
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


def test_daily_metrics_and_scoring():
    m = daily_metrics(_daily())
    assert m and m["price"] > 0 and m["turnover20"] > 0 and m["vol_ratio"] > 2 and 0 <= m["close_pos"] <= 1
    p = PremarketParams()
    s, reasons, why = score_stock(m, p)
    assert why == "" and 0 < s <= 100 and any("거래대금" in r for r in reasons) and any("거래량" in r for r in reasons)
    # 제외 조건
    assert score_stock({**m, "price": 1_000}, p)[2].startswith("가격")
    assert score_stock({**m, "turnover20": 1e8}, p)[2].startswith("거래대금")
    assert score_stock({**m, "atr_pct": 15.0}, p)[2].startswith("변동성")
    assert score_stock(m, p, stock_sent=-0.6)[2].startswith("종목 악재")
    assert score_stock(m, p, sector_sent=0.5)[0] > score_stock(m, p, sector_sent=-0.5)[0]
    assert daily_metrics(_daily(10)) is None


def test_sim_selection_is_deterministic_per_day_and_saves(tmp_path: Path):
    cfg = dict(DEFAULTS)
    cfg["watchlist"] = dict(DEFAULTS["watchlist"])
    p = PremarketParams(size=8, pinned=["005930"])
    sel = UniverseSelector(cfg, p, None, None, "sim", str(tmp_path)).select()
    assert len(sel.picks) == 8 and sel.picks[0].code == "005930"  # 고정 종목 우선 포함
    assert sel.candidates >= 30 and all(pk.reasons for pk in sel.picks)
    assert (tmp_path / f"universe_{sel.date.replace('-', '')}.json").exists()
    again = UniverseSelector(cfg, p, None, None, "sim", str(tmp_path)).load_today()
    assert again is not None and [x.code for x in again.picks] == [x.code for x in sel.picks]
    scores = [pk.score for pk in sel.picks]
    assert scores == sorted(scores, reverse=True) or sel.picks[0].code == "005930"


def test_should_run_and_next_run():
    p = PremarketParams(time="08:50")
    assert should_run_premarket(datetime(2026, 9, 23, 8, 50, tzinfo=KST), p, None)  # 수요일
    assert not should_run_premarket(datetime(2026, 9, 23, 8, 49, tzinfo=KST), p, None)
    assert not should_run_premarket(datetime(2026, 9, 23, 9, 31, tzinfo=KST), p, None)
    assert not should_run_premarket(datetime(2026, 9, 23, 8, 55, tzinfo=KST), p, date(2026, 9, 23))
    assert not should_run_premarket(datetime(2026, 9, 26, 8, 55, tzinfo=KST), p, None)  # 토요일
    assert not should_run_premarket(datetime(2026, 9, 23, 8, 55, tzinfo=KST), PremarketParams(enabled=False), None)
    assert next_run_text(p, datetime(2026, 9, 25, 9, 0, tzinfo=KST)) == "09/28 08:50"  # 금요일 이후 → 월요일


def test_build_params_from_config():
    cfg = dict(DEFAULTS)
    cfg["premarket"] = {**DEFAULTS["premarket"], "pinned": "5930, 000660", "size": 12}
    p = build_premarket_params(cfg)
    assert p.pinned == ["005930", "000660"] and p.size == 12


@pytest.fixture
def ctl(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("watchlist:\n  '005930': 삼성전자\n  '000660': SK하이닉스\n  '035720': 카카오\nfeed: sim\nsim:\n  speed: 0\n  history_days: 1\npremarket:\n  size: 6\n  pinned: ['000660']\nlog_dir: " + str(tmp_path) + "\n", encoding="utf-8")
    c = AppController(str(cfg))
    yield c
    c._sched_stop.set()


def test_premarket_apply_replace_and_merge(ctl):
    info = ctl.premarket_run(force=True, feed="sim", wait=True)
    assert info["status"] == "done" and info["today_done"]
    wl = ctl.cfg["watchlist"]
    picks = [p["code"] for p in info["selection"]["picks"]]
    assert "000660" in wl and set(picks) <= set(wl) and len(wl) == len(set(picks) | {"000660"})
    assert "035720" not in wl or "035720" in picks  # replace: 미선정 종목은 제거
    ctl.set_settings({"premarket.mode": "merge"})
    ctl.watchlist_update(add={"035720": "카카오"})
    info2 = ctl.premarket_run(force=True, feed="sim", wait=True)
    assert "035720" in ctl.cfg["watchlist"] and info2["status"] == "done"
    assert ctl.premarket_run(force=False)["today_done"]  # 이미 오늘 선정 → 재실행 안 함


def test_universe_api_and_chat(ctl):
    port = find_free_port("127.0.0.1", 18820)
    srv = AppServer(ctl, "127.0.0.1", port)
    srv.start()
    try:
        d = json.loads(urllib.request.urlopen(srv.url + "/api/universe", timeout=10).read())
        assert d["enabled"] and d["time"] == "08:50" and d["today_done"] is False
        req = urllib.request.Request(srv.url + "/api/universe/run", data=json.dumps({"force": True, "feed": "sim", "wait": True}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        d = json.loads(urllib.request.urlopen(req, timeout=120).read())
        assert d["status"] == "done" and len(d["selection"]["picks"]) == 6
        req = urllib.request.Request(srv.url + "/api/chat", data=json.dumps({"message": "선정 종목 보여줘"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
        assert "선정" in r["reply"] and "1." in r["reply"] and r["data"]["navigate"] == "reco"
        st = json.loads(urllib.request.urlopen(srv.url + "/api/status", timeout=10).read())
        assert st["premarket"]["today_done"] and st["premarket"]["picks"] == 6
    finally:
        srv.stop()
