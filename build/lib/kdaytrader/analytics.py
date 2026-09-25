"""gs-quant(Goldman Sachs, Apache-2.0) 의 timeseries 분석 계층을 참고해 순수 pandas/numpy 로 옮긴 모듈.

원본: https://github.com/goldmansachs/gs-quant — gs_quant.timeseries.{statistics, econometrics, technicals, analysis,
backtesting}. GS Marquee API 의존성을 제거하고, 일중 분봉(DatetimeIndex, 1일 390봉)에 맞게 연율화 계수를 조정했다.

| 함수                       | gs-quant 원본                                  | 용도                                   |
|---------------------------|-----------------------------------------------|----------------------------------------|
| zscores                   | statistics.zscores                            | 롤링 z-score (평균회귀 트리거)             |
| winsorize                 | statistics.winsorize                          | 극단값 제한 (거래량 비율 등)               |
| exponential_std           | statistics.exponential_std                    | 지수가중 표준편차                          |
| exponential_volatility    | technicals.exponential_volatility             | 지수가중 실현변동성(연율화 %)              |
| smoothed_moving_average   | technicals.smoothed_moving_average            | Wilder 식 SMMA                          |
| returns / prices          | econometrics.returns / prices                 | 단순·로그 수익률 ↔ 가격 복원               |
| volatility                | econometrics.volatility                       | 롤링 실현변동성(연율화 %)                  |
| beta / correlation        | econometrics.beta / correlation               | 벤치마크(코스피) 대비 롤링 베타·상관        |
| max_drawdown              | econometrics.max_drawdown                     | 롤링 최대낙폭                              |
| sharpe_ratio              | econometrics.sharpe_ratio                     | 연율화 샤프                               |
| percentiles               | statistics.percentiles                        | 롤링 백분위                               |
| smooth_spikes             | analysis.smooth_spikes                        | 스파이크 틱 제거                           |
| rolling_linear_regression | statistics.RollingLinearRegression            | 페어 헤지비율/R² (statsmodels 없이)        |
| backtest_basket           | timeseries.backtesting.backtest_basket        | 가중 바스켓(섹터 바스켓) 성과               |
| event_study               | timeseries.event_study.event_impact_analysis  | 지수 급변 이벤트 전후 종목 반응             |
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

# 일중 1분봉 기준 연율화 계수: 252일 × 390봉
BARS_PER_DAY = 390
ANNUALIZATION_DAILY = 252
ANNUALIZATION_MINUTE = 252 * BARS_PER_DAY


class Returns(str, Enum):
    SIMPLE = "simple"
    LOGARITHMIC = "log"


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def zscores(x: pd.Series, w: int | None = None) -> pd.Series:
    """롤링 z-score. w=None 이면 전체 표본 기준 (ddof=1, gs-quant 와 동일)."""
    if x.size < 2:
        return pd.Series(0.0, index=x.index)
    if not w:
        mu, sd = x.mean(), x.std(ddof=1)
        return (x - mu) / sd if sd > 0 else pd.Series(0.0, index=x.index)
    mu = x.rolling(w, min_periods=2).mean()
    sd = x.rolling(w, min_periods=2).std(ddof=1)
    return (x - mu) / sd.replace(0.0, np.nan)


def winsorize(x: pd.Series, limit: float = 2.5, w: int | None = None) -> pd.Series:
    """평균 ± limit·표준편차 범위로 값을 제한. w 를 주면 롤링 기준."""
    if x.size < 1:
        return x
    if not w:
        mu, sd = x.mean(), x.std(ddof=1)
        if not sd or np.isnan(sd):
            return x
        return x.clip(lower=mu - sd * limit, upper=mu + sd * limit)
    mu = x.rolling(w, min_periods=2).mean()
    sd = x.rolling(w, min_periods=2).std(ddof=1)
    return x.clip(lower=mu - sd * limit, upper=mu + sd * limit)


def exponential_std(x: pd.Series, beta: float = 0.75) -> pd.Series:
    """지수가중 표준편차. beta 는 과거 가중치(0 ≤ beta < 1)."""
    return x.ewm(alpha=1.0 - beta, adjust=False).std()


def percentiles(x: pd.Series, w: int | None = None) -> pd.Series:
    """각 시점 값이 (롤링) 표본에서 차지하는 백분위 (0~100)."""
    if not w:
        return x.rank(pct=True) * 100.0
    return x.rolling(w, min_periods=2).apply(lambda a: (a[:-1] < a[-1]).mean() * 100.0 if len(a) > 1 else 50.0, raw=True)


def smoothed_moving_average(x: pd.Series, w: int) -> pd.Series:
    """Wilder 식 수정이동평균: P_t = ((N-1)·P_{t-1} + x_t)/N."""
    return x.ewm(alpha=1.0 / w, adjust=False, min_periods=w).mean()


@dataclass
class RollingRegressionResult:
    beta: pd.Series
    alpha: pd.Series
    r_squared: pd.Series
    resid_std: pd.Series


def rolling_linear_regression(x: pd.Series, y: pd.Series, w: int) -> RollingRegressionResult:
    """y = alpha + beta·x 롤링 OLS (statsmodels 없이). gs-quant RollingLinearRegression 대응."""
    df = pd.concat([x.rename("x"), y.rename("y")], axis=1, join="inner").dropna()
    xm = df["x"].rolling(w, min_periods=w).mean()
    ym = df["y"].rolling(w, min_periods=w).mean()
    cov = (df["x"] * df["y"]).rolling(w, min_periods=w).mean() - xm * ym
    var = (df["x"] ** 2).rolling(w, min_periods=w).mean() - xm**2
    beta = cov / var.replace(0.0, np.nan)
    alpha = ym - beta * xm
    fitted = alpha + beta * df["x"]
    resid = df["y"] - fitted
    ss_res = (resid**2).rolling(w, min_periods=w).sum()
    ss_tot = ((df["y"] - ym) ** 2).rolling(w, min_periods=w).sum()
    r2 = 1.0 - ss_res / ss_tot.replace(0.0, np.nan)
    resid_std = resid.rolling(w, min_periods=w).std(ddof=2)
    return RollingRegressionResult(beta, alpha, r2, resid_std)


# ---------------------------------------------------------------------------
# econometrics
# ---------------------------------------------------------------------------
def returns(x: pd.Series, obs: int = 1, kind: Returns = Returns.SIMPLE) -> pd.Series:
    if kind == Returns.LOGARITHMIC:
        return np.log(x / x.shift(obs))
    return x / x.shift(obs) - 1.0


def prices(r: pd.Series, initial: float = 100.0, kind: Returns = Returns.SIMPLE) -> pd.Series:
    r = r.fillna(0.0)
    if kind == Returns.LOGARITHMIC:
        return initial * np.exp(r.cumsum())
    return initial * (1.0 + r).cumprod()


def _ann_factor(x: pd.Series, intraday: bool | None = None) -> float:
    if intraday is None:
        if isinstance(x.index, pd.DatetimeIndex) and len(x) >= 2:
            step = (x.index[1] - x.index[0]).total_seconds()
            intraday = step < 6 * 3600
        else:
            intraday = False
    return ANNUALIZATION_MINUTE if intraday else ANNUALIZATION_DAILY


def annualize(x: pd.Series, intraday: bool | None = None) -> pd.Series:
    return x * math.sqrt(_ann_factor(x, intraday))


def volatility(x: pd.Series, w: int | None = None, kind: Returns = Returns.SIMPLE, intraday: bool | None = None) -> pd.Series:
    """롤링 실현변동성(연율화 %). gs-quant econometrics.volatility."""
    r = returns(x, 1, kind)
    sd = r.rolling(w, min_periods=2).std(ddof=1) if w else r.expanding(min_periods=2).std(ddof=1)
    return annualize(sd, intraday) * 100.0


def exponential_volatility(x: pd.Series, beta: float = 0.75, intraday: bool | None = None) -> pd.Series:
    """지수가중 실현변동성(연율화 %). gs-quant technicals.exponential_volatility."""
    return annualize(exponential_std(returns(x), beta), intraday) * 100.0


def beta(x: pd.Series, b: pd.Series, w: int | None = None, is_prices: bool = True) -> pd.Series:
    """벤치마크 대비 롤링 베타 = Cov(R, S)/Var(S). 초기 3개 값은 NaN (표본 부족)."""
    rx = returns(x) if is_prices else x
    rb = returns(b) if is_prices else b
    df = pd.concat([rx.rename("x"), rb.rename("b")], axis=1, join="inner").dropna()
    if w:
        cov = df["x"].rolling(w, min_periods=2).cov(df["b"])
        var = df["b"].rolling(w, min_periods=2).var()
    else:
        cov = df["x"].expanding(min_periods=2).cov(df["b"])
        var = df["b"].expanding(min_periods=2).var()
    out = cov / var.replace(0.0, np.nan)
    out.iloc[:3] = np.nan
    return out.reindex(x.index)


def correlation(x: pd.Series, y: pd.Series, w: int | None = None, is_prices: bool = True) -> pd.Series:
    rx = returns(x) if is_prices else x
    ry = returns(y) if is_prices else y
    df = pd.concat([rx.rename("x"), ry.rename("y")], axis=1, join="inner").dropna()
    out = df["x"].rolling(w, min_periods=3).corr(df["y"]) if w else df["x"].expanding(min_periods=3).corr(df["y"])
    return out.reindex(x.index)


def max_drawdown(x: pd.Series, w: int | None = None) -> pd.Series:
    """롤링 최대낙폭 (음수, -0.2 = 20% 낙폭)."""
    if w:
        rolling_max = x.rolling(w, min_periods=1).max()
        return (x / rolling_max - 1.0).rolling(w, min_periods=1).min()
    return (x / x.cummax() - 1.0).cummin()


def drawdown_duration(x: pd.Series) -> int:
    """현재 고점 이후 경과 봉 수 (신고점이면 0)."""
    if x.empty:
        return 0
    peak_pos = int(np.argmax(x.to_numpy()))
    return len(x) - 1 - peak_pos


def sharpe_ratio(x: pd.Series, risk_free: float = 0.0, intraday: bool | None = None, is_prices: bool = True) -> float:
    r = returns(x).dropna() if is_prices else x.dropna()
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    f = _ann_factor(x, intraday)
    excess = r - risk_free / f
    return float(excess.mean() / r.std(ddof=1) * math.sqrt(f))


def sortino_ratio(r: pd.Series, ann_factor: float = ANNUALIZATION_DAILY) -> float:
    r = r.dropna()
    downside = r[r < 0]
    if len(r) < 2 or len(downside) == 0 or downside.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / downside.std(ddof=1) * math.sqrt(ann_factor))


def calmar_ratio(total_return_pct: float, max_dd_pct: float, n_days: int) -> float:
    if max_dd_pct >= 0 or n_days <= 0:
        return 0.0
    ann_ret = ((1 + total_return_pct / 100.0) ** (252.0 / n_days) - 1.0) * 100.0
    return float(ann_ret / abs(max_dd_pct))


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------
def smooth_spikes(x: pd.Series, threshold: float = 0.01, absolute: bool = False) -> pd.Series:
    """양옆 이웃보다 (1±threshold)배 넘게 튀는 점을 이웃 평균으로 치환 (잘못 찍힌 틱 제거)."""
    if len(x) < 3:
        return x
    v = x.to_numpy(dtype=float).copy()
    prev, nxt = v[:-2], v[2:]
    cur = v[1:-1]
    if absolute:
        higher = (cur > prev + threshold) & (cur > nxt + threshold)
        lower = (prev > cur + threshold) & (nxt > cur + threshold)
    else:
        m = 1.0 + threshold
        higher = (cur > prev * m) & (cur > nxt * m)
        lower = (prev > cur * m) & (nxt > cur * m)
    spike = higher | lower
    cur = np.where(spike, (prev + nxt) / 2.0, cur)
    v[1:-1] = cur
    return pd.Series(v, index=x.index)


def consecutive(x: pd.Series, positive: bool = True) -> int:
    """마지막 시점 기준 연속 양(음)의 변화 횟수."""
    d = np.sign(x.diff().dropna().to_numpy())
    target = 1 if positive else -1
    n = 0
    for s in d[::-1]:
        if s == target:
            n += 1
        else:
            break
    return n


# ---------------------------------------------------------------------------
# backtesting / event study
# ---------------------------------------------------------------------------
def backtest_basket(series: list[pd.Series], weights: list[float] | None = None, costs: list[float] | None = None, rebal_every: int = 1) -> tuple[pd.Series, pd.DataFrame]:
    """가중 바스켓 성과(100 기준)와 실제 비중. gs-quant backtest_basket 의 일중 버전 (rebal_every 봉마다 리밸런스)."""
    n_assets = len(series)
    weights = weights or [1.0 / n_assets] * n_assets
    costs = costs or [0.0] * n_assets
    df = pd.concat([s.rename(i) for i, s in enumerate(series)], axis=1, join="inner").dropna()
    if df.empty:
        return pd.Series(dtype=float), pd.DataFrame()
    px = df.to_numpy(dtype=float)
    w = np.array(weights, dtype=float)
    c = np.array(costs, dtype=float)
    n = len(df)
    out = np.zeros(n)
    units = np.zeros((n, n_assets))
    actual = np.zeros((n, n_assets))
    out[0] = 100.0
    units[0] = out[0] * w / px[0]
    actual[0] = w
    prev_rebal = 0
    for i in range(1, n):
        out[i] = out[i - 1] + np.dot(units[i - 1], px[i] - px[i - 1])
        actual[i] = w * (px[i] / px[prev_rebal]) * (out[prev_rebal] / out[i])
        if rebal_every > 0 and i % rebal_every == 0:
            out[i] -= np.dot(c, np.abs(w - actual[i])) * out[i]
            units[i] = out[i] * w / px[i]
            prev_rebal = i
            actual[i] = w
        else:
            units[i] = units[i - 1]
    return pd.Series(out, index=df.index), pd.DataFrame(actual, index=df.index, columns=range(n_assets))


def event_study(asset: pd.Series, driver: pd.Series, threshold: float = 0.01, window: int = 10, direction: str | None = None) -> pd.DataFrame:
    """driver(예: 코스피) 의 1봉 수익률이 |threshold| 를 넘는 이벤트를 찾고, 전후 window 봉 동안 asset 의 누적 수익률을 정렬.

    반환: index = 이벤트 시각, columns = -window..+window 의 누적 수익률(%). gs-quant event_impact_analysis 의 단순화.
    direction: 'up' | 'down' | None(양방향)
    """
    r = returns(driver).dropna()
    if direction == "up":
        events = r[r >= threshold].index
    elif direction == "down":
        events = r[r <= -threshold].index
    else:
        events = r[r.abs() >= threshold].index
    rows = {}
    pos = {ts: i for i, ts in enumerate(asset.index)}
    a = asset.to_numpy(dtype=float)
    for ev in events:
        if ev not in pos:
            continue
        i = pos[ev]
        if i - window < 0 or i + window >= len(a):
            continue
        base = a[i - 1] if i >= 1 else a[i]
        seg = a[i - window : i + window + 1] / base * 100.0 - 100.0
        rows[ev] = seg
    if not rows:
        return pd.DataFrame(columns=list(range(-window, window + 1)))
    return pd.DataFrame.from_dict(rows, orient="index", columns=list(range(-window, window + 1)))


def event_response_summary(study: pd.DataFrame) -> dict:
    if study.empty:
        return {"events": 0}
    horizons = [h for h in (1, 3, 5, 10) if h in study.columns]
    return {"events": int(len(study)), **{f"+{h}봉 평균(%)": round(float(study[h].mean()), 3) for h in horizons}, **{f"+{h}봉 승률(%)": round(float((study[h] > 0).mean() * 100), 1) for h in horizons}}
