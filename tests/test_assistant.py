"""챗봇 도우미 규칙 해석기 테스트 (네트워크 없이)."""
import json
import urllib.request
from pathlib import Path

import pytest

from kdaytrader.app import AppController, AppServer, find_free_port
from kdaytrader.assistant import Assistant, HELP_TOPICS


@pytest.fixture
def ctl(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("watchlist:\n  '005930': 삼성전자\n  '000660': SK하이닉스\nsim:\n  speed: 0\n  history_days: 1\nlog_dir: " + str(tmp_path) + "\n", encoding="utf-8")
    return AppController(str(cfg))


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def test_add_top_parses_market_count_and_calls_screener(ctl, monkeypatch):
    calls = {}

    def fake_top(market, n, by, exclude, kis):
        calls.update(market=market, n=n, by=by)
        return {f"1{i:05d}": f"종목{i}" for i in range(n)}

    monkeypatch.setattr("kdaytrader.screener.top_codes", fake_top)
    a = Assistant(ctl)
    r = a.rules("코스닥 상위 100개 관심종목에 추가해줘")
    assert calls == {"market": "KOSDAQ", "n": 100, "by": "volume"} and "100개" in r.reply
    assert len(ctl.cfg["watchlist"]) == 102
    r = a.rules("코스피 시가총액 top 5 넣어줘")
    assert calls["market"] == "KOSPI" and calls["by"] == "marketcap" and calls["n"] == 5


def test_add_and_remove_by_name_and_code(ctl):
    a = Assistant(ctl)
    r = a.rules("관심종목에 카카오, 035420 추가")
    assert "035720" in ctl.cfg["watchlist"] and "035420" in ctl.cfg["watchlist"]
    r = a.rules("카카오 빼줘")
    assert "035720" not in ctl.cfg["watchlist"] and "삭제" in r.reply
    r = a.rules("관심종목 보여줘")
    assert "삼성전자" in r.reply and "005930" in r.reply
    r = a.rules("관심종목 전부 삭제")
    assert r.needs_confirm and r.needs_confirm["payload"]
    r = a.rules(r.needs_confirm["payload"], confirm=True)
    assert ctl.cfg["watchlist"] == {}


def test_settings_by_natural_language(ctl):
    a = Assistant(ctl)
    assert "저장" in a.rules("손절 ATR 배수 2로 바꿔줘").reply and ctl.cfg["risk"]["atr_stop_mult"] == 2.0
    a.rules("매수 임계값 60")
    assert ctl.cfg["strategy"]["buy_threshold"] == 60.0
    a.rules("1회 손실 0.5%로")
    assert abs(ctl.cfg["risk"]["risk_per_trade"] - 0.005) < 1e-9
    a.rules("최대 종목 3개")
    assert ctl.cfg["risk"]["max_positions"] == 3
    a.rules("진입 시작 시각 09:20")
    assert ctl.cfg["risk"]["entry_start"] == "09:20"
    r = a.rules("캔들 주기 0분")
    assert "실패" in r.reply or "오류" in r.reply


def test_help_topics_and_fallback(ctl):
    a = Assistant(ctl)
    r = a.rules("시그널 점수는 뭐야?")
    assert "점수" in r.reply and "임계" in r.reply
    r = a.rules("KIS 연동 방법 알려줘")
    assert "apiportal" in r.reply
    r = a.rules("blahblah 알 수 없는 요청")
    assert "이렇게 말씀해" in r.reply
    assert len(HELP_TOPICS) >= 12


def test_engine_control_and_queries_via_chat_api(ctl):
    port = find_free_port("127.0.0.1", 18800)
    srv = AppServer(ctl, "127.0.0.1", port)
    srv.start()
    try:
        r = _post(srv.url + "/api/chat", {"message": "상태 알려줘"})
        assert "엔진: 대기 중" in r["reply"] and r["source"] == "rules"
        r = _post(srv.url + "/api/chat", {"message": "추천 종목 알려줘"})
        assert "실행 중이 아니" in r["reply"]
        r = _post(srv.url + "/api/chat", {"message": "시뮬레이션 시작해"})
        assert "시작했습니다" in r["reply"] and r["data"]["navigate"] == "dash"
        r = _post(srv.url + "/api/chat", {"message": "시작해"})
        assert "이미 실행 중" in r["reply"]
        r = _post(srv.url + "/api/chat", {"message": "자동매매 꺼줘"})
        assert "껐습니다" in r["reply"] and ctl.engine.trader.auto_trade is False
        r = _post(srv.url + "/api/chat", {"message": "전량 청산해줘"})
        assert "포지션이 없습니다" in r["reply"] or r["needs_confirm"]
        r = _post(srv.url + "/api/chat", {"message": "카카오 추가하고 상태 알려줘"})
        assert "추가" in r["reply"] and "현재 상태" in r["reply"] and "035720" in ctl.engine.watchlist
        r = _post(srv.url + "/api/chat", {"message": "엔진 정지"})
        assert "정지" in r["reply"] or "실행 중이 아닙니다" in r["reply"]  # 최대 배속 시뮬레이션은 이미 끝났을 수 있음
        r = _post(srv.url + "/api/chat", {"message": "오늘 수익률 어때?"})
        assert "성과" in r["reply"] or "없습니다" in r["reply"]
        r = _post(srv.url + "/api/chat", {"message": "3일 백테스트 돌려줘"})
        assert "백테스트를 시작" in r["reply"]
        s = json.loads(urllib.request.urlopen(srv.url + "/api/chat/suggestions", timeout=10).read())
        assert len(s["suggestions"]) >= 4 and s["llm"] is False
    finally:
        srv.stop()
