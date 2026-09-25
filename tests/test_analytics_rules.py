from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from kdaytrader import analytics as A
from kdaytrader.data.base import candles_to_df
from kdaytrader.data.sim import generate_history
from kdaytrader.indicators import compute_all
from kdaytrader.market import KST
from kdaytrader.strategy.rules import (Action, ActionType, AggregateTrigger, MeanReversionTrigger, MktTrigger, NotTrigger, RiskTrigger, RuleSet, TimeWindowTrigger, TriggerDirection, build_ruleset, default_ruleset)


def _prices(n=400, seed=0, vol=0.001):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-09-23 09:00", periods=n, freq="1min", tz=KST)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, vol, n))), index=idx)


def test_zscores_and_winsorize():
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 100.0])
    z = A.zscores(x)
    assert abs(z.iloc[-1]) > 1.5 and abs(z.mean()) < 1e-9
    w = A.winsorize(x, 1.0)
    assert w.iloc[-1] < 100.0 and w.iloc[0] >= x.mean() - x.std()
    rz = A.zscores(_prices(), 60)
    assert rz.iloc[:1].isna().all() and rz.dropna().abs().max() < 6


def test_returns_prices_roundtrip():
    p = _prices(50)
    r = A.returns(p)
    back = A.prices(r, initial=p.iloc[0])
    assert np.allclose(back.to_numpy(), p.to_numpy())
    lr = A.returns(p, kind=A.Returns.LOGARITHMIC)
    assert np.allclose(A.prices(lr, p.iloc[0], A.Returns.LOGARITHMIC).to_numpy(), p.to_numpy())


def test_volatility_annualization_intraday_vs_daily():
    p = _prices(400, vol=0.001)
    v_intra = A.volatility(p, 120).dropna().iloc[-1]
    daily = p.copy()
    daily.index = pd.date_range("2025-01-01", periods=len(p), freq="B")
    v_daily = A.volatility(daily, 120).dropna().iloc[-1]
    assert v_intra > v_daily * 10  # 분봉 연율화 계수가 훨씬 큼
    ev = A.exponential_volatility(p, 0.9).dropna()
    assert (ev > 0).all()


def test_beta_and_correlation_recover_known_beta():
    rng = np.random.default_rng(3)
    n = 600
    rb = rng.normal(0, 0.001, n)
    rx = 1.5 * rb + rng.normal(0, 0.0002, n)
    idx = pd.date_range("2026-09-23 09:00", periods=n, freq="1min", tz=KST)
    b = pd.Series(100 * np.exp(np.cumsum(rb)), index=idx)
    x = pd.Series(100 * np.exp(np.cumsum(rx)), index=idx)
    beta = A.beta(x, b, 300).dropna()
    assert abs(beta.iloc[-1] - 1.5) < 0.15
    assert beta.index[0] > idx[3]  # 초기 3개는 NaN
    corr = A.correlation(x, b, 300).dropna()
    assert corr.iloc[-1] > 0.9


def test_max_drawdown_and_duration():
    p = pd.Series([100, 110, 99, 105, 120, 108.0])
    mdd = A.max_drawdown(p)
    assert abs(mdd.iloc[-1] - (99 / 110 - 1)) < 1e-9
    assert A.drawdown_duration(p) == 1
    rolling = A.max_drawdown(p, 2)
    assert rolling.iloc[-1] <= 0


def test_ratios():
    r = pd.Series([0.01, -0.005, 0.02, -0.01, 0.015])
    assert A.sortino_ratio(r) > 0
    assert A.calmar_ratio(10.0, -5.0, 126) > 0
    assert A.calmar_ratio(10.0, 0.0, 126) == 0.0
    assert A.sharpe_ratio(_prices(100)) == A.sharpe_ratio(_prices(100))


def test_smooth_spikes_and_consecutive():
    x = pd.Series([100, 100.5, 130, 100.8, 101.0])
    s = A.smooth_spikes(x, 0.05)
    assert abs(s.iloc[2] - (100.5 + 100.8) / 2) < 1e-9
    assert A.consecutive(pd.Series([1, 2, 3, 4, 3, 4, 5, 6]), True) == 3
    assert A.consecutive(pd.Series([5, 4, 3]), False) == 2


def test_rolling_regression_matches_beta():
    rng = np.random.default_rng(5)
    x = pd.Series(rng.normal(0, 1, 300))
    y = 2.0 * x + 0.5 + rng.normal(0, 0.1, 300)
    res = A.rolling_linear_regression(x, y, 100)
    assert abs(res.beta.dropna().iloc[-1] - 2.0) < 0.1
    assert abs(res.alpha.dropna().iloc[-1] - 0.5) < 0.1
    assert res.r_squared.dropna().iloc[-1] > 0.95


def test_backtest_basket_equal_weights_and_costs():
    a = pd.Series([100, 110, 121.0], index=pd.date_range("2026-01-01", periods=3, freq="B"))
    b = pd.Series([100, 100, 100.0], index=a.index)
    out, w = A.backtest_basket([a, b], [0.5, 0.5], rebal_every=0)
    assert abs(out.iloc[-1] - 110.5) < 1e-9  # 50% 가 21% 상승
    out_c, _ = A.backtest_basket([a, b], [0.5, 0.5], costs=[0.01, 0.01], rebal_every=1)
    assert out_c.iloc[-1] < out.iloc[-1]


def test_event_study_aligns_windows():
    idx = pd.date_range("2026-09-23 09:00", periods=100, freq="1min", tz=KST)
    driver = pd.Series(100.0, index=idx)
    driver.iloc[50:] = 98.0  # -2% 이벤트 (이후 유지)
    asset = pd.Series(np.linspace(100, 110, 100), index=idx)
    study = A.event_study(asset, driver, 0.01, 5, "down")
    assert len(study) == 1 and list(study.columns) == list(range(-5, 6))
    assert study.iloc[0][5] > 0
    summ = A.event_response_summary(study)
    assert summ["events"] == 1 and "+5봉 평균(%)" in summ
    assert A.event_study(asset, driver, 0.01, 5, "up").empty


# ----- 규칙 엔진 -----
def _ind():
    df = candles_to_df(generate_history("005930", 2, 70_000, seed=4))
    return compute_all(df)


def test_mkt_and_time_triggers():
    ind = _ind()
    now = datetime(2026, 9, 23, 9, 5, tzinfo=KST)
    rsi = float(ind["rsi"].iloc[-1])
    assert MktTrigger("rsi", rsi - 1, TriggerDirection.ABOVE).has_triggered(ind, False, now)
    assert not MktTrigger("rsi", rsi + 1, TriggerDirection.ABOVE).has_triggered(ind, False, now)
    assert not MktTrigger("없는컬럼", 0, TriggerDirection.ABOVE).has_triggered(ind, False, now)
    assert TimeWindowTrigger(time(9, 0), time(9, 10)).has_triggered(ind, False, now)
    assert not TimeWindowTrigger(time(9, 0), time(9, 10)).has_triggered(ind, False, now.replace(hour=11))


def test_mean_reversion_trigger_states():
    n = 100
    idx = pd.date_range("2026-09-23 09:00", periods=n, freq="1min", tz=KST)
    close = pd.Series(np.concatenate([np.full(n - 1, 100.0) + np.sin(np.arange(n - 1)) * 0.5, [90.0]]), index=idx)
    ind = pd.DataFrame({"close": close})
    t = MeanReversionTrigger("close", 2.0, 60, 60)
    now = idx[-1].to_pydatetime()
    info = t.has_triggered(ind, False, now)
    assert info and info.scaling == 1.0 and "저평가" in info.reason
    ind2 = pd.DataFrame({"close": pd.concat([close.iloc[:-1], pd.Series([100.6], index=[idx[-1]])])})
    info2 = t.has_triggered(ind2, True, now)
    assert info2 and info2.scaling == -1.0  # 평균 복귀 → 청산
    ind3 = pd.DataFrame({"close": pd.concat([close.iloc[:-1], pd.Series([112.0], index=[idx[-1]])])})
    assert t.has_triggered(ind3, False, now).scaling == -1.0  # 고평가


def test_risk_aggregate_not_and_ruleset():
    ind = _ind()
    now = datetime(2026, 9, 23, 10, 0, tzinfo=KST)
    rt = RiskTrigger("r_multiple", -1.5, TriggerDirection.BELOW, actions=[Action(ActionType.EXIT)], name="손실한도")
    rs = RuleSet([rt])
    assert rs.evaluate(ind, True, now, {"r_multiple": -2.0}).exit_hint.startswith("규칙 손실한도")
    assert rs.evaluate(ind, True, now, {"r_multiple": -1.0}).exit_hint == ""
    assert rs.evaluate(ind, False, now, {}).exit_hint == ""
    always = TimeWindowTrigger(time(0, 0), time(23, 59))
    never = TimeWindowTrigger(time(23, 58), time(23, 59))
    assert AggregateTrigger([always, never], "any").has_triggered(ind, False, now)
    assert not AggregateTrigger([always, never], "all").has_triggered(ind, False, now)
    assert NotTrigger(never).has_triggered(ind, False, now)
    rs2 = RuleSet([TimeWindowTrigger(time(0, 0), time(23, 59), scaling=-1.0, actions=[Action(ActionType.SCORE, 10.0)]), MktTrigger("rsi", -1, TriggerDirection.ABOVE, actions=[Action(ActionType.BLOCK_ENTRY)], name="차단")])
    out = rs2.evaluate(ind, False, now)
    assert out.bias == -10.0 and out.entry_block == "규칙 차단" and len(out.notes) == 2


def test_build_ruleset_from_config_and_defaults():
    cfg = [
        {"kind": "mkt", "column": "adx", "level": 20, "direction": "above", "actions": [{"type": "score", "score": 5}]},
        {"kind": "mean_reversion", "z_score_bound": 2.5, "actions": [{"type": "score", "score": 10}]},
        {"kind": "risk", "measure": "holding_min", "level": 90, "direction": "above", "actions": [{"type": "exit"}]},
        {"kind": "time", "start": "14:30", "end": "15:00", "actions": [{"type": "block_entry"}]},
        {"kind": "all", "triggers": [{"kind": "time", "start": "09:00", "end": "10:00"}], "actions": [{"type": "score", "score": 1}]},
        {"kind": "not", "trigger": {"kind": "time", "start": "09:00", "end": "10:00"}, "actions": []},
    ]
    rs = build_ruleset(cfg)
    assert len(rs.triggers) == 6
    assert len(default_ruleset().triggers) == 4
    ind = _ind()
    out = default_ruleset().evaluate(ind, False, datetime(2026, 9, 23, 9, 3, tzinfo=KST))
    assert any("개장 직후" in n for n in out.notes)
