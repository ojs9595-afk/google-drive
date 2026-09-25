from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from kdaytrader.data.base import Candle, Tick, candles_to_df
from kdaytrader.data.candles import CandleStore
from kdaytrader.data.sim import generate_history
from kdaytrader.indicators import compute_all
from kdaytrader.market import KST
from kdaytrader.risk import RiskManager, RiskParams
from kdaytrader.strategy.quant import MicroState, QuantParams, QuantSignals, formulaic_alphas, pairs_signal, tsmom_signal, vol_regime


def test_pairs_signal_detects_stretched_spread():
    rng = np.random.default_rng(1)
    n = 200
    x = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    noise = np.zeros(n)
    # OU 노이즈 (평균회귀)
    for i in range(1, n):
        noise[i] = 0.9 * noise[i - 1] + rng.normal(0, 0.0006)
    y = x * 1.5 * np.exp(noise)
    y_stretched = y.copy()
    y_stretched[-1] *= np.exp(-0.03)  # 마지막에 크게 저평가
    z, beta, hl, corr = pairs_signal(pd.Series(y_stretched), pd.Series(x), QuantParams())
    assert corr > 0.5
    assert abs(beta - 1.0) < 0.3
    assert z < -2.0
    assert 1 < hl < 60


def test_tsmom_sign():
    up = pd.Series(np.linspace(100, 110, 200))
    atr = pd.Series(np.full(200, 0.2))
    assert tsmom_signal(up, atr, (20, 60, 120)) > 0.5
    assert tsmom_signal(up[::-1].reset_index(drop=True), atr, (20, 60, 120)) < -0.5


def test_formulaic_alphas_bounded():
    df = candles_to_df(generate_history("005930", 1, 70_000, seed=2))
    a, parts = formulaic_alphas(df, 60)
    assert -1 <= a <= 1
    assert set(parts) == {"a101", "a12", "a9", "a53"}


def test_vol_regime_scaling():
    rng = np.random.default_rng(0)
    calm = rng.normal(0, 0.001, 150)
    wild = rng.normal(0, 0.004, 30)
    close = pd.Series(100 * np.exp(np.cumsum(np.concatenate([calm, wild]))))
    regime, ratio, scale = vol_regime(close, 20, 120, 1.5, 0.7)
    assert regime == "high" and ratio > 1.5 and 0.5 <= scale < 0.7
    regime2, _, scale2 = vol_regime(pd.Series(100 * np.exp(np.cumsum(calm))), 20, 120, 1.5, 0.7)
    assert regime2 in ("normal", "low") and 0.5 <= scale2 <= 1.5


def test_micro_state_updates_from_ticks():
    m = MicroState()
    t0 = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    for i in range(5):
        m.update(Tick("A", t0 + timedelta(seconds=i), 100, 10, 100 * (i + 1), ask=101, bid=100, strength=140, buy_vol=80 * (i + 1), sell_vol=20 * (i + 1), ask_qty=1000, bid_qty=3000), 0.5)
    assert m.available
    assert m.obi > 0.4 and m.tfi > 0.5 and m.strength > 120 and m.micro_dev > 0


def test_quant_signals_end_to_end_and_strategy_weights():
    store = CandleStore(60)
    data = {c: generate_history(c, 2, p, seed=3) for c, p in {"005930": 70_000, "000660": 180_000, "035420": 200_000}.items()}
    for c, cs in data.items():
        store.seed(c, cs)
    q = QuantSignals(store, QuantParams(), pairs={"005930": "000660"}, sectors={"005930": "반도체", "000660": "반도체"})
    t0 = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    for i in range(6):
        q.on_tick(Tick("005930", t0, 70_000, 10, 100, ask=70_100, bid=70_000, strength=130, buy_vol=100 * (i + 1), sell_vol=50 * (i + 1), ask_qty=500, bid_qty=1500))
    for c in data:
        ind = compute_all(store.df(c, n=420))
        snap = q.compute(c, ind)
    snap = q.compute("005930", compute_all(store.df("005930", n=420)))
    assert q.partner_of("005930") == "000660"
    assert q.partner_of("035420") == ""
    assert "tsmom" in snap.components and "alpha" in snap.components and "micro" in snap.components
    assert "xs_momentum" in snap.components  # 유니버스 3종목
    assert snap.partner == "000660" and snap.pairs_z == snap.pairs_z
    assert snap.components["micro"][0] > 0
    # 전략에 quant 를 주입하면 구성요소가 점수에 반영된다
    from kdaytrader.strategy import EnsembleStrategy

    s = EnsembleStrategy()
    ind = compute_all(store.df("005930", n=420), s.p.indicator_params())
    base = s.evaluate_with_indicators("005930", ind)
    with_q = s.evaluate_with_indicators("005930", ind, quant=snap)
    assert "micro" in with_q.components and "micro" not in base.components


def test_kelly_scale_from_trade_history():
    rm = RiskManager(RiskParams(risk_per_trade=0.01, kelly_lookback=20))
    now = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    rm.new_day(now.date(), 10_000_000)
    assert rm.kelly_scale() == 1.0  # 이력 부족
    for i in range(12):
        rm.record_trade("A", -10_000, now)
    assert rm.kelly_scale() == 0.5  # 전패 → 최소 배율
    rm.new_day(now.date() + timedelta(days=1), 10_000_000)
    assert len(rm.stats.pnl_history) == 12  # 일자 바뀌어도 이력 유지
    for i in range(20):
        rm.record_trade("A", 30_000 if i % 3 else -10_000, now)
    assert rm.kelly_scale() == 1.5  # 승률 67%, 손익비 3 → 상한
    rm2 = RiskManager(RiskParams(kelly_enabled=False))
    assert rm2.kelly_scale() == 1.0
