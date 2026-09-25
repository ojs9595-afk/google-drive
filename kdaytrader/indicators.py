"""기술적 지표 (numpy/pandas 기반, 외부 TA 라이브러리 의존 없음).

모든 함수는 pandas Series/DataFrame 을 받아 같은 길이의 Series 를 돌려준다.
DataFrame 은 open/high/low/close/volume 컬럼과 DatetimeIndex 를 가진다고 가정한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------- 이동평균 ----------
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def slope(s: pd.Series, n: int = 3) -> pd.Series:
    """n 캔들 전 대비 변화율 (%)."""
    return (s / s.shift(n) - 1.0) * 100.0


# ---------- 모멘텀 ----------
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    out[avg_gain.isna() | avg_loss.isna()] = np.nan
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    line = fast_ema - slow_ema
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return line, sig, hist


def stochastic(df: pd.DataFrame, k: int = 14, d: int = 3, smooth: int = 3):
    low_min = df["low"].rolling(k, min_periods=k).min()
    high_max = df["high"].rolling(k, min_periods=k).max()
    rng = (high_max - low_min).replace(0.0, np.nan)
    fast_k = (df["close"] - low_min) / rng * 100.0
    slow_k = fast_k.rolling(smooth, min_periods=1).mean()
    slow_d = slow_k.rolling(d, min_periods=1).mean()
    return slow_k, slow_d


# ---------- 변동성 ----------
def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n, min_periods=n).mean()
    std = close.rolling(n, min_periods=n).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    width = (upper - lower) / mid.replace(0.0, np.nan) * 100.0
    pct_b = (close - lower) / (upper - lower).replace(0.0, np.nan)
    return mid, upper, lower, width, pct_b


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0):
    """SuperTrend 라인과 방향(+1 상승 / -1 하락)."""
    hl2 = (df["high"] + df["low"]) / 2.0
    a = atr(df, period)
    upper_basic = (hl2 + mult * a).to_numpy()
    lower_basic = (hl2 - mult * a).to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    line = np.full(n, np.nan)
    direction = np.zeros(n, dtype=int)
    for i in range(n):
        if np.isnan(upper_basic[i]):
            continue
        if i == 0 or np.isnan(upper[i - 1]):
            upper[i] = upper_basic[i]
            lower[i] = lower_basic[i]
            direction[i] = 1
            line[i] = lower[i]
            continue
        upper[i] = upper_basic[i] if (upper_basic[i] < upper[i - 1] or close[i - 1] > upper[i - 1]) else upper[i - 1]
        lower[i] = lower_basic[i] if (lower_basic[i] > lower[i - 1] or close[i - 1] < lower[i - 1]) else lower[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < lower[i] else 1
        else:
            direction[i] = 1 if close[i] > upper[i] else -1
        line[i] = lower[i] if direction[i] == 1 else upper[i]
    return pd.Series(line, index=df.index), pd.Series(direction, index=df.index)


def adx(df: pd.DataFrame, n: int = 14):
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(df)
    atr_n = tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().replace(0.0, np.nan)
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr_n
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr_n
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan) * 100.0
    adx_v = dx.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    return adx_v, plus_di, minus_di


# ---------- 거래량 ----------
def vwap(df: pd.DataFrame) -> pd.Series:
    """일중 VWAP (일자별로 누적을 초기화)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    day = df.index.normalize() if isinstance(df.index, pd.DatetimeIndex) else pd.Series(0, index=df.index)
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    out = cum_pv / cum_v
    return out.fillna(df["close"])


def volume_ratio(volume: pd.Series, n: int = 20) -> pd.Series:
    avg = volume.shift(1).rolling(n, min_periods=max(3, n // 2)).mean().replace(0.0, np.nan)
    return volume / avg


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def opening_range(df: pd.DataFrame, minutes: int = 30):
    """일자별 시가 후 N분 구간의 고가/저가. 구간 이전 캔들은 NaN."""
    if not isinstance(df.index, pd.DatetimeIndex) or len(df) == 0:
        nan = pd.Series(np.nan, index=df.index)
        return nan, nan
    day = df.index.normalize()
    minutes_since = (df.index - day).total_seconds() / 60.0 - 9 * 60
    in_range = (minutes_since >= 0) & (minutes_since < minutes)
    hi = df["high"].where(in_range)
    lo = df["low"].where(in_range)
    or_high = hi.groupby(day).cummax().groupby(day).ffill()
    or_low = lo.groupby(day).cummin().groupby(day).ffill()
    # 구간이 끝난 뒤에만 유효. 구간 중에는 NaN 처리해서 미리 진입하지 않도록 한다.
    done = minutes_since >= minutes
    return or_high.where(done), or_low.where(done)


# ---------- 일괄 계산 ----------
def compute_all(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """전략에 필요한 모든 지표를 한 번에 붙인 DataFrame 을 반환."""
    p = {
        "ema_fast": 9,
        "ema_slow": 21,
        "ema_trend": 50,
        "rsi": 14,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "bb": 20,
        "bb_k": 2.0,
        "atr": 14,
        "st_period": 10,
        "st_mult": 3.0,
        "adx": 14,
        "vol_n": 20,
        "orb_minutes": 30,
        "stoch_k": 14,
        "stoch_d": 3,
    }
    if params:
        p.update(params)
    out = df.copy()
    c = out["close"]
    out["ema_fast"] = ema(c, p["ema_fast"])
    out["ema_slow"] = ema(c, p["ema_slow"])
    out["ema_trend"] = ema(c, p["ema_trend"])
    out["ema_slow_slope"] = slope(out["ema_slow"], 3)
    out["rsi"] = rsi(c, p["rsi"])
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(c, p["macd_fast"], p["macd_slow"], p["macd_signal"])
    out["bb_mid"], out["bb_upper"], out["bb_lower"], out["bb_width"], out["bb_pct"] = bollinger(c, p["bb"], p["bb_k"])
    out["atr"] = atr(out, p["atr"])
    out["st_line"], out["st_dir"] = supertrend(out, p["st_period"], p["st_mult"])
    out["adx"], out["plus_di"], out["minus_di"] = adx(out, p["adx"])
    out["vwap"] = vwap(out)
    out["vol_ratio"] = volume_ratio(out["volume"], p["vol_n"])
    out["stoch_k"], out["stoch_d"] = stochastic(out, p["stoch_k"], p["stoch_d"])
    out["or_high"], out["or_low"] = opening_range(out, p["orb_minutes"])
    return out
