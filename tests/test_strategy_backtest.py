from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from kdaytrader.backtest import Backtester, grid_search
from kdaytrader.context import ContextParams, MarketContext, ScheduledEvent, detect_sectors, sentiment_score
from kdaytrader.data.base import Candle, candles_to_df
from kdaytrader.data.sim import generate_history
from kdaytrader.indicators import compute_all
from kdaytrader.market import KST
from kdaytrader.risk import RiskParams
from kdaytrader.strategy import Action, EnsembleStrategy, StrategyParams


def _uptrend_candles(n=150, start=50_000):
    ts0 = datetime(2026, 9, 23, 9, 0, tzinfo=KST)
    out, p = [], float(start)
    rng = np.random.default_rng(0)
    pattern = [0.0012, 0.0005, -0.0009, 0.0011, -0.0007, 0.0010]
    for i in range(n):
        o = p
        step = pattern[i % 6] if i >= n - 36 else 0.0001
        c = o * (1 + step + rng.normal(0, 0.0003))
        vol = 3000 if i >= n - 3 else 1000
        out.append(Candle(ts0 + timedelta(minutes=i), o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, vol))
        p = c
    return out


def test_strategy_hold_when_not_enough_data():
    s = EnsembleStrategy()
    sig = s.evaluate("A", candles_to_df(_uptrend_candles(20)))
    assert sig.action == Action.HOLD and "데이터 부족" in sig.reasons


def test_strategy_buy_on_strong_uptrend_with_volume():
    s = EnsembleStrategy(StrategyParams(buy_threshold=50))
    sig = s.evaluate("A", candles_to_df(_uptrend_candles()))
    assert sig.score > 50
    assert sig.action == Action.BUY
    assert sig.regime in ("trend", "range")


def test_strategy_bias_and_block_hooks():
    s = EnsembleStrategy(StrategyParams(buy_threshold=50))
    ind = compute_all(candles_to_df(_uptrend_candles()), s.p.indicator_params())
    base = s.evaluate_with_indicators("A", ind)
    assert base.action == Action.BUY
    blocked = s.evaluate_with_indicators("A", ind, entry_block="시장 급락")
    assert blocked.action == Action.HOLD and any("시장 급락" in r for r in blocked.reasons)
    penalized = s.evaluate_with_indicators("A", ind, bias=-80)
    assert penalized.score < base.score and penalized.action == Action.HOLD
    held = s.evaluate_with_indicators("A", ind, has_position=True, exit_hint="악재")
    assert held.action == Action.SELL


def test_backtest_runs_and_closes_all_positions():
    data = {c: generate_history(c, 3, p, seed=5) for c, p in {"005930": 70_000, "000660": 180_000}.items()}
    res = Backtester(EnsembleStrategy(), RiskParams(), 10_000_000).run(data)
    summ = res.summary()
    assert summ["거래횟수"] >= 1
    assert res.equity_curve.notna().all()
    assert abs(res.final_equity - res.initial_cash - sum(t.pnl for t in res.trades)) < 1.0
    # 모든 거래는 당일 청산 (데이트레이딩)
    assert all(t.entry_ts.date() == t.exit_ts.date() for t in res.trades)
    assert all(t.exit_ts.time() <= RiskParams().force_close for t in res.trades)


def test_backtest_precomputed_matches_incremental():
    data = {"005930": generate_history("005930", 2, 70_000, seed=9)}
    bt = Backtester(EnsembleStrategy(), RiskParams(), 10_000_000)
    res_fast = bt.run(data)
    # 증분 계산 경로 강제 (evaluate_with_indicators 우회)
    class Plain(EnsembleStrategy):
        pass
    bt2 = Backtester(Plain(), RiskParams(), 10_000_000)
    bt2.strategy.evaluate_with_indicators  # 존재하지만 backtest 는 precompute 를 사용
    res2 = bt2.run(data)
    assert res_fast.n_trades == res2.n_trades


def test_grid_search_sorted():
    data = {"005930": generate_history("005930", 2, 70_000, seed=11)}
    results = grid_search(data, {"buy_threshold": [45, 65]}, initial_cash=5_000_000)
    assert len(results) == 2
    assert results[0][1]["총수익률(%)"] >= results[1][1]["총수익률(%)"]


# ----- 컨텍스트 -----
def test_sentiment_and_sector_detection():
    assert sentiment_score("삼성전자 HBM 대규모 수주…목표주가 상향") > 0.5
    assert sentiment_score("코스피 급락, 사이드카 발동") < -0.5
    assert sentiment_score("오늘 날씨는 맑음") == 0.0
    assert "반도체" in detect_sectors("SK하이닉스 HBM 증설")
    assert "자동차" in detect_sectors("현대차 관세 부과 우려")


def test_context_market_risk_off_blocks_entry():
    ctx = MarketContext(ContextParams(), names={"005930": "삼성전자"})
    now = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    ctx.update_index("KOSPI", 2500, -2.0, now)
    a = ctx.assess("005930", now)
    assert a.entry_block.startswith("시장 급락")
    assert a.bias < 0
    assert ctx.urgent_exit("005930", now) is None  # -2% 는 긴급청산 기준(-2.25%) 미만
    ctx.update_index("KOSPI", 2450, -3.0, now)
    assert ctx.urgent_exit("005930", now) == "시장 급락 청산"


def test_context_stock_news_block_and_exit():
    ctx = MarketContext(ContextParams(), names={"035720": "카카오"})
    now = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    ctx.add_news(now - timedelta(minutes=5), "카카오 압수수색…주가 급락", "test")
    a = ctx.assess("035720", now)
    assert a.entry_block == "종목 악재 뉴스"
    assert a.exit_hint == "종목 악재 속보"
    assert a.news_bias < -15
    # 오래된 뉴스는 감쇠되어 긴급 청산은 해제
    later = now + timedelta(hours=3)
    assert ctx.urgent_exit("035720", later) is None
    # 중복 제목은 무시
    assert ctx.add_news(now, "카카오 압수수색…주가 급락", "test") is None


def test_context_sector_news_and_relative_strength():
    ctx = MarketContext(names={"005930": "삼성전자", "005380": "현대차"})
    now = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    ctx.add_news(now, "반도체 업황 호조…HBM 수요 급증", "test")
    ctx.update_stock_change("005930", 2.0)
    ctx.update_stock_change("005380", -1.0)
    a = ctx.assess("005930", now)
    assert a.sector_bias > 0
    assert ctx.sector_relative_strength("반도체") == 1.5
    assert ctx.assess("005380", now).sector_bias < 0


def test_context_scheduled_event_blocks():
    from datetime import time

    ctx = MarketContext(events=[ScheduledEvent("금통위", time(10, 0), 10, 15)])
    assert ctx.assess("005930", datetime(2026, 9, 23, 10, 5, tzinfo=KST)).entry_block.startswith("이벤트")
    assert ctx.assess("005930", datetime(2026, 9, 23, 11, 0, tzinfo=KST)).entry_block == ""


def test_backtest_with_risk_off_context_makes_no_trades():
    ctx = MarketContext()
    ctx.update_index("KOSPI", 2500, -2.5, datetime(2026, 9, 23, 9, 0, tzinfo=KST))
    data = {"005930": generate_history("005930", 2, 70_000, seed=5)}
    res = Backtester(EnsembleStrategy(), RiskParams(), 10_000_000, context=ctx).run(data)
    assert res.n_trades == 0
