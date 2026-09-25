"""1차 감사 최종 확정 항목 회귀 테스트."""
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from kdaytrader.context import ContextParams, MarketContext, detect_sectors, sentiment_score
from kdaytrader.data.base import Tick
from kdaytrader.data.candles import CandleStore
from kdaytrader.data.news import parse_rss
from kdaytrader.market import KST
from kdaytrader.strategy.quant import MicroState, QuantSignals
from kdaytrader.strategy.rules import Action, ActionType, AggregateTrigger, NotTrigger, RiskTrigger, RuleSet, TimeWindowTrigger, TriggerDirection


def test_compound_negative_terms_keep_sign():
    assert sentiment_score("환율 급등") < 0 and sentiment_score("국제유가 급등, WTI 90달러") < 0
    assert sentiment_score("물가 급등에 소비 위축") < 0 and sentiment_score("실업률 상승") < 0
    assert sentiment_score("삼성전자 HBM 대규모 수주…목표주가 상향") > 0.5 and sentiment_score("코스피 급락, 사이드카 발동") < -0.5


def test_sector_keyword_latin_boundary():
    assert "엔터" not in detect_sectors("SMR 소형원자로 수주")  # 'SM' 오탐 방지
    assert "통신" not in detect_sectors("KTX 요금 인상") and "통신" not in detect_sectors("KT&G 실적")
    assert "엔터" in detect_sectors("SM 엔터테인먼트 신인 데뷔") and "통신" in detect_sectors("KT 5G 투자")


def test_news_dedup_keeps_recent_titles_and_zero_decay_safe():
    ctx = MarketContext(ContextParams(news_half_life_min=1.0), names={})
    now = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    for i in range(5200):
        ctx.add_news(now, f"뉴스 제목 {i} 급등", "t")
    assert ctx.add_news(now, "뉴스 제목 5199 급등", "t") is None  # 최근 제목은 여전히 중복으로 인식
    assert ctx.add_news(now, "뉴스 제목 1 급등", "t") is not None  # 오래된 제목은 제거됨
    far = now + timedelta(days=30)
    s, n = ctx.news_sentiment(far)  # 완전히 감쇠(0.0) → 0 나눗셈 없이 (0, 0)
    assert (s, n) == (0.0, 0)


def test_microstate_ignores_ticks_without_microstructure():
    m = MicroState()
    t0 = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    for i in range(10):
        m.update(Tick("A", t0, 100.0, 1, 10 * i), 0.2)  # 네이버 폴링처럼 호가/체결강도 없음
    assert not m.available and m.samples == 0
    m.update(Tick("A", t0, 100.0, 1, 100, ask=101, bid=100, strength=120, ask_qty=100, bid_qty=300), 0.2)
    assert m.samples == 1


def test_pairs_partner_lookup_does_not_create_builder():
    store = CandleStore(60)
    store.seed("005930", [])
    q = QuantSignals(store, pairs={"005930": "000660"})
    df = pd.DataFrame({"open": [1.0] * 30, "high": [1.0] * 30, "low": [1.0] * 30, "close": [1.0] * 30, "volume": [1] * 30, "atr": [0.01] * 30}, index=pd.date_range("2026-09-23 09:00", periods=30, freq="1min", tz=KST))
    q.compute("005930", df)
    assert not store.has("000660") and store.codes() == ["005930"]


def test_nested_risk_trigger_and_exit_scaling():
    ind = pd.DataFrame({"close": [1.0] * 5})
    now = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    nested = AggregateTrigger([RiskTrigger("r_multiple", -1.0, TriggerDirection.BELOW), TimeWindowTrigger(time(0, 0), time(23, 59))], "all", actions=[Action(ActionType.EXIT)], name="중첩")
    out = RuleSet([nested]).evaluate(ind, True, now, {"r_multiple": -2.0})
    assert out.exit_hint.startswith("규칙 중첩")
    inside_not = NotTrigger(RiskTrigger("r_multiple", -1.0, TriggerDirection.BELOW), actions=[Action(ActionType.EXIT)], name="부정")
    assert RuleSet([inside_not]).evaluate(ind, True, now, {"r_multiple": -2.0}).exit_hint == ""
    assert RuleSet([inside_not]).evaluate(ind, True, now, {"r_multiple": 0.5}).exit_hint.startswith("규칙 부정")
    plain = TimeWindowTrigger(time(0, 0), time(23, 59), scaling=1.0, actions=[Action(ActionType.EXIT)], name="시간청산")
    assert RuleSet([plain]).evaluate(ind, True, now).exit_hint.startswith("규칙 시간청산")


def test_orb_breakout_detected_on_first_bar_after_window():
    from kdaytrader.data.base import Candle, candles_to_df
    from kdaytrader.indicators import compute_all
    from kdaytrader.strategy import EnsembleStrategy

    ts0 = datetime(2026, 9, 23, 9, 0, tzinfo=KST)
    cs = []
    for i in range(80):
        base = 100.0 + (i * 0.02 if i < 30 else 0.6)
        c = base + (3.0 if i == 30 else 0.0)  # 30번째 봉(구간 종료 직후)에 시가범위 돌파
        cs.append(Candle(ts0 + timedelta(minutes=i), base, max(base, c) + 0.05, min(base, c) - 0.05, c, 5000 if i == 30 else 1000))
    s = EnsembleStrategy()
    ind = compute_all(candles_to_df(cs), s.p.indicator_params())
    row_prev, row = ind.iloc[29], ind.iloc[30]
    assert np.isnan(row_prev.or_high) and not np.isnan(row.or_high)
    score, why = s._breakout(row, row_prev, ind.iloc[:31])
    assert any("ORB" in w for w in why)


def test_parse_rss_euc_kr_declaration():
    xml = '<?xml version="1.0" encoding="euc-kr"?><rss><channel><item><title>코스피 급등</title><link>http://x</link></item></channel></rss>'.encode("euc-kr")
    items = parse_rss(xml, "t")
    assert len(items) == 1 and items[0][1] == "코스피 급등"
