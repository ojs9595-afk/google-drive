import numpy as np
import pandas as pd

from kdaytrader.data.base import candles_to_df
from kdaytrader.data.sim import generate_history
from kdaytrader import indicators as ind


def _df(n_days=2):
    return candles_to_df(generate_history("005930", n_days, 70_000, seed=3))


def test_rsi_bounds_and_direction():
    df = _df()
    r = ind.rsi(df["close"], 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert ind.rsi(up, 14).iloc[-1] > 90


def test_ema_sma_lengths_and_nan_warmup():
    df = _df()
    e = ind.ema(df["close"], 9)
    s = ind.sma(df["close"], 9)
    assert len(e) == len(df)
    assert e.iloc[:8].isna().all() and not np.isnan(e.iloc[8])
    assert s.iloc[:8].isna().all()


def test_bollinger_ordering():
    df = _df()
    mid, up, lo, width, pct = ind.bollinger(df["close"])
    valid = ~mid.isna()
    assert (up[valid] >= mid[valid]).all() and (mid[valid] >= lo[valid]).all()
    assert (width[valid] >= 0).all()


def test_vwap_resets_daily():
    df = _df(2)
    v = ind.vwap(df)
    day = df.index.normalize()
    first_of_day2 = df[day == day.unique()[1]].index[0]
    tp = (df.loc[first_of_day2, "high"] + df.loc[first_of_day2, "low"] + df.loc[first_of_day2, "close"]) / 3
    assert abs(v.loc[first_of_day2] - tp) < 1e-6


def test_supertrend_direction_values():
    df = _df()
    line, d = ind.supertrend(df)
    assert set(d.unique()) <= {-1, 0, 1}
    assert d.iloc[-1] in (-1, 1)


def test_adx_range():
    df = _df()
    a, p, m = ind.adx(df)
    a = a.dropna()
    assert ((a >= 0) & (a <= 100)).all()


def test_opening_range_only_after_window():
    df = _df(1)
    hi, lo = ind.opening_range(df, 30)
    assert hi.iloc[:30].isna().all()
    assert not np.isnan(hi.iloc[30])
    assert hi.iloc[30] == df["high"].iloc[:30].max()
    assert lo.iloc[30] == df["low"].iloc[:30].min()


def test_compute_all_columns():
    out = ind.compute_all(_df())
    for c in ["ema_fast", "ema_slow", "rsi", "macd_hist", "bb_upper", "atr", "st_dir", "adx", "vwap", "vol_ratio", "or_high"]:
        assert c in out.columns
