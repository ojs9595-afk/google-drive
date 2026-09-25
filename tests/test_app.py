import json
import time
import urllib.request
from pathlib import Path

import pytest

from kdaytrader.app import AppController, AppServer, find_free_port


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    cfg = tmp_path_factory.mktemp("cfg") / "config.yaml"
    cfg.write_text("watchlist:\n  '005930': 삼성전자\n  '000660': SK하이닉스\nsim:\n  speed: 0\n  history_days: 2\nlog_dir: " + str(tmp_path_factory.mktemp("logs")) + "\n", encoding="utf-8")
    ctl = AppController(str(cfg))
    port = find_free_port("127.0.0.1", 18787)
    srv = AppServer(ctl, "127.0.0.1", port)
    srv.start()
    yield srv, ctl, cfg
    srv.stop()


def test_status_and_static(server):
    srv, ctl, _ = server
    st = _get(srv.url + "/api/status")
    assert st["running"] is False and "clock" in st and st["watchlist_count"] == 2
    html = urllib.request.urlopen(srv.url + "/", timeout=10).read().decode("utf-8")
    assert "K-DayTrader" in html and "/static/app.js" in html
    js = urllib.request.urlopen(srv.url + "/static/app.js", timeout=10).read()
    assert len(js) > 1000
    assert urllib.request.urlopen(srv.url + "/static/app.css", timeout=10).status == 200


def test_config_roundtrip(server):
    srv, ctl, cfg_path = server
    c = _get(srv.url + "/api/config")
    assert c["watchlist"]["005930"] == "삼성전자" and "_schema" in c
    c["watchlist"]["035420"] = "NAVER"
    c["risk"] = {"risk_per_trade": 0.02, "entry_start": "09:10"}
    c["kis"] = {"app_key": "k", "app_secret": "s", "account": "12345678-01", "paper": True}
    code, saved = _post(srv.url + "/api/config", c)
    assert code == 200 and saved["watchlist"]["035420"] == "NAVER" and saved["risk"]["risk_per_trade"] == 0.02
    assert "035420" in cfg_path.read_text(encoding="utf-8")
    assert ctl.status()["kis_configured"] is True
    bad = dict(c)
    bad["risk"] = {"entry_start": "not-a-time"}
    code, err = _post(srv.url + "/api/config", bad)
    assert code == 400 and "error" in err


def test_engine_lifecycle_chart_state(server):
    srv, ctl, _ = server
    code, r = _post(srv.url + "/api/start", {"feed": "sim", "mode": "paper", "news": False, "sim_speed": 0})
    assert code == 200 and r["run"]["feed"] == "sim"
    code, dup = _post(srv.url + "/api/start", {"feed": "sim"})
    assert code == 400 and "실행 중" in dup["error"]
    deadline = time.time() + 60
    st = {}
    while time.time() < deadline:
        st = _get(srv.url + "/api/state")
        if st.get("watchlist") and st["watchlist"][0]["price"] > 0 and any(w.get("score") for w in st["watchlist"]):
            break
        time.sleep(0.5)
    assert st.get("watchlist") and st["equity"] > 0
    rc = st["recommendations"]
    assert set(rc) >= {"buy", "watch", "sell", "buy_threshold", "sell_threshold", "universe"} and rc["universe"] >= 1
    for item in rc["buy"] + rc["watch"] + rc["sell"]:
        assert item["suggest"]["stop"] <= item["price"] <= item["suggest"]["take_profit"] and item["label"]
    assert isinstance(st["signal_log"], list) and "bars" in st and "feed_health" in st
    perf = _get(srv.url + "/api/performance?days=30")
    assert "kpi" in perf and perf["kpi"]["trades"] >= 0 and isinstance(perf["open_positions"], list)
    csv_txt = urllib.request.urlopen(srv.url + "/api/performance.csv", timeout=10).read().decode("utf-8-sig")
    assert csv_txt.startswith("진입시각,청산시각")
    ch = _get(srv.url + f"/api/chart?code=005930&n=120")
    assert ch["code"] == "005930" and len(ch["bars"]["t"]) > 50 and len(ch["bars"]["close"]) == len(ch["bars"]["t"])
    assert all(v is None or isinstance(v, (int, float)) for v in ch["bars"]["rsi"])
    code, err = _post(srv.url + "/api/chart", {})
    assert code == 404 and "error" in err
    try:
        urllib.request.urlopen(srv.url + "/api/chart?code=999999", timeout=10)
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400
    code, ta = _post(srv.url + "/api/toggle_auto", {})
    assert code == 200 and "auto_trade" in ta
    # 시뮬레이션은 speed 0 이라 곧 끝난다 → 정지 요청은 성공해야 함
    code, stop = _post(srv.url + "/api/stop", {})
    assert code == 200 and stop["ok"] is True
    time.sleep(0.5)
    assert _get(srv.url + "/api/status")["running"] is False
    logs = _get(srv.url + "/api/logs")
    assert logs["last_id"] >= 1


def test_backtest_job(server):
    srv, ctl, _ = server
    code, r = _post(srv.url + "/api/backtest", {"days": 2, "codes": "005930", "feed": "sim", "seed": 3})
    assert code == 200
    deadline = time.time() + 120
    j = {}
    while time.time() < deadline:
        j = _get(srv.url + "/api/backtest")
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert j["status"] == "done", j.get("error")
    res = j["result"]
    assert "총수익률(%)" in res["summary"] and len(res["equity"]) > 10 and len(res["drawdown"]) == len(res["equity"])
    assert res["params"]["codes"] == ["005930"]


def test_news_offline_is_graceful(server):
    srv, ctl, _ = server
    d = _get(srv.url + "/api/news?fetch=0")
    assert d["news_count"] == 0 and isinstance(d["assessments"], list) and len(d["assessments"]) >= 2
    assert all(a["entry_block"] == "" for a in d["assessments"])
