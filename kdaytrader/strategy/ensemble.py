"""다중 지표 앙상블 데이트레이딩 전략.

핵심 아이디어
- 시장 국면(regime)을 ADX 로 판정: 추세장(trend) / 횡보장(range).
- 추세장에서는 추세추종·돌파 신호 가중치를 높이고, 횡보장에서는 평균회귀 신호 가중치를 높인다.
- 각 하위 신호는 -1 ~ +1 점수를 내며, 가중합을 -100 ~ +100 으로 정규화한다.
- 거래량 급증은 신호의 신뢰도를 키우는 승수(multiplier)로 작용한다.
- 매수: score >= buy_threshold 이고 필수 필터(VWAP 위, 거래대금 등) 통과.
- 매도(청산): 보유 중 score <= sell_threshold 이거나 추세 이탈 신호.

이 전략은 '최고 수익률'을 보장하지 않는다. 반드시 백테스트·모의투자로 검증 후 사용해야 한다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..indicators import compute_all
from .base import Action, Signal, Strategy


@dataclass
class StrategyParams:
    # 지표 파라미터
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    rsi: int = 14
    bb: int = 20
    bb_k: float = 2.0
    atr: int = 14
    st_period: int = 10
    st_mult: float = 3.0
    adx: int = 14
    vol_n: int = 20
    orb_minutes: int = 30
    # 국면 판정
    adx_trend: float = 25.0
    # 임계값
    buy_threshold: float = 55.0
    sell_threshold: float = -35.0
    # 가중치 (추세장 / 횡보장)
    w_trend: tuple[float, float] = (1.0, 0.5)
    w_momentum: tuple[float, float] = (1.0, 0.8)
    w_breakout: tuple[float, float] = (1.2, 0.4)
    w_reversion: tuple[float, float] = (0.2, 1.2)
    w_supertrend: tuple[float, float] = (1.0, 0.5)
    # 퀀트 알파 가중치 (추세장 / 횡보장) — strategy/quant.py 참조
    w_xs_momentum: tuple[float, float] = (0.8, 0.4)
    w_tsmom: tuple[float, float] = (0.8, 0.3)
    w_pairs: tuple[float, float] = (0.3, 1.0)
    w_micro: tuple[float, float] = (0.8, 0.8)
    w_alpha: tuple[float, float] = (0.6, 0.6)
    high_vol_threshold_add: float = 5.0  # 고변동성 국면에서 매수 임계값 가산
    # 필터
    require_above_vwap: bool = True  # 매수 시 VWAP 위 요구
    min_vol_ratio: float = 0.8  # 직전 20봉 평균 대비 최소 거래량 비율
    max_rsi_entry: float = 78.0  # 과매수 구간 추격 금지
    min_bb_width_pct: float = 0.15  # 밴드폭이 너무 좁으면(변동성 없음) 진입 회피

    def indicator_params(self) -> dict:
        d = asdict(self)
        keys = ["ema_fast", "ema_slow", "ema_trend", "rsi", "bb", "bb_k", "atr", "st_period", "st_mult", "adx", "vol_n", "orb_minutes"]
        return {k: d[k] for k in keys}


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, x)))


def _f(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return f


class EnsembleStrategy(Strategy):
    name = "ensemble"

    def __init__(self, params: StrategyParams | None = None):
        self.p = params or StrategyParams()

    @property
    def min_bars(self) -> int:
        return max(self.p.ema_trend, self.p.bb, self.p.adx * 2, 30) + 5

    # ----- 하위 신호 -----
    def _trend(self, r: pd.Series, prev: pd.Series) -> tuple[float, list[str]]:
        s, why = 0.0, []
        if r.close > r.vwap:
            s += 0.3
        else:
            s -= 0.3
        if r.ema_fast > r.ema_slow:
            s += 0.3
            if prev.ema_fast <= prev.ema_slow:
                s += 0.2
                why.append("EMA 골든크로스")
        else:
            s -= 0.3
            if prev.ema_fast >= prev.ema_slow:
                s -= 0.2
                why.append("EMA 데드크로스")
        if not np.isnan(r.ema_trend):
            s += 0.2 if r.close > r.ema_trend else -0.2
        if not np.isnan(r.ema_slow_slope):
            s += _clip(r.ema_slow_slope / 0.5, -0.2, 0.2)
        if s >= 0.6:
            why.append("추세 상승(VWAP/EMA 정배열)")
        elif s <= -0.6:
            why.append("추세 하락(VWAP/EMA 역배열)")
        return _clip(s), why

    def _momentum(self, r: pd.Series, prev: pd.Series) -> tuple[float, list[str]]:
        s, why = 0.0, []
        if not np.isnan(r.macd_hist):
            if r.macd_hist > 0:
                s += 0.3
                if r.macd_hist > prev.macd_hist:
                    s += 0.2
                if prev.macd_hist <= 0:
                    s += 0.2
                    why.append("MACD 히스토그램 양전환")
            else:
                s -= 0.3
                if r.macd_hist < prev.macd_hist:
                    s -= 0.2
                if prev.macd_hist >= 0:
                    s -= 0.2
                    why.append("MACD 히스토그램 음전환")
        if not np.isnan(r.rsi):
            if 50 <= r.rsi <= 70:
                s += 0.3
            elif 70 < r.rsi <= 80:
                s += 0.1
            elif r.rsi > 80:
                s -= 0.3
                why.append(f"RSI 과매수({r.rsi:.0f})")
            elif 40 <= r.rsi < 50:
                s -= 0.1
            elif r.rsi < 40:
                s -= 0.3
            if r.rsi > prev.rsi:
                s += 0.1
            else:
                s -= 0.1
        if not np.isnan(r.stoch_k) and not np.isnan(r.stoch_d):
            if r.stoch_k > r.stoch_d and prev.stoch_k <= prev.stoch_d and r.stoch_k < 80:
                s += 0.2
                why.append("스토캐스틱 골든크로스")
            elif r.stoch_k < r.stoch_d and prev.stoch_k >= prev.stoch_d and r.stoch_k > 20:
                s -= 0.2
        return _clip(s), why

    def _breakout(self, r: pd.Series, prev: pd.Series, df: pd.DataFrame) -> tuple[float, list[str]]:
        s, why = 0.0, []
        vr = r.vol_ratio if not np.isnan(r.vol_ratio) else 1.0
        # 시가 범위(ORB) 돌파
        if not np.isnan(r.or_high):
            if r.close > r.or_high and prev.close <= prev.or_high:
                s += 0.7 if vr >= 1.5 else 0.4
                why.append("시가범위(ORB) 상향 돌파")
            elif r.close > r.or_high:
                s += 0.2
            if r.close < r.or_low and prev.close >= prev.or_low:
                s -= 0.7 if vr >= 1.5 else 0.4
                why.append("시가범위(ORB) 하향 이탈")
            elif r.close < r.or_low:
                s -= 0.2
        # 볼린저 밴드 스퀴즈 후 확장 돌파
        if not np.isnan(r.bb_width):
            recent_width = df["bb_width"].iloc[-30:-1]
            squeeze = len(recent_width) >= 10 and recent_width.min() <= r.bb_width * 0.6
            if r.close > r.bb_upper and squeeze and vr >= 1.3:
                s += 0.5
                why.append("볼린저 스퀴즈 상향 돌파")
            elif r.close < r.bb_lower and squeeze and vr >= 1.3:
                s -= 0.5
                why.append("볼린저 스퀴즈 하향 이탈")
        # 최근 20봉 신고가/신저가 돌파
        if len(df) >= 21:
            hh = df["high"].iloc[-21:-1].max()
            ll = df["low"].iloc[-21:-1].min()
            if r.close > hh and vr >= 1.2:
                s += 0.3
                why.append("20봉 신고가 돌파")
            elif r.close < ll and vr >= 1.2:
                s -= 0.3
        return _clip(s), why

    def _reversion(self, r: pd.Series, prev: pd.Series) -> tuple[float, list[str]]:
        """횡보장 평균회귀. 밴드 하단+과매도에서 반등 캔들이면 매수, 반대면 매도."""
        s, why = 0.0, []
        if np.isnan(r.bb_pct) or np.isnan(r.rsi):
            return 0.0, why
        bullish_candle = r.close > r.open and r.close > prev.close
        bearish_candle = r.close < r.open and r.close < prev.close
        if r.bb_pct <= 0.05 and r.rsi <= 32 and bullish_candle:
            s += 1.0
            why.append("밴드 하단 과매도 반등")
        elif r.bb_pct <= 0.15 and r.rsi <= 40:
            s += 0.4
        if r.bb_pct >= 0.95 and r.rsi >= 68 and bearish_candle:
            s -= 1.0
            why.append("밴드 상단 과매수 반락")
        elif r.bb_pct >= 0.85 and r.rsi >= 60:
            s -= 0.4
        return _clip(s), why

    def _supertrend(self, r: pd.Series, prev: pd.Series) -> tuple[float, list[str]]:
        why = []
        if r.st_dir == 0:
            return 0.0, why
        s = 0.6 if r.st_dir > 0 else -0.6
        if r.st_dir > 0 and prev.st_dir <= 0:
            s = 1.0
            why.append("슈퍼트렌드 상승 전환")
        elif r.st_dir < 0 and prev.st_dir >= 0:
            s = -1.0
            why.append("슈퍼트렌드 하락 전환")
        return s, why

    # ----- 종합 -----
    def evaluate_with_indicators(
        self,
        code: str,
        ind: pd.DataFrame,
        has_position: bool = False,
        bias: float = 0.0,
        entry_block: str = "",
        exit_hint: str = "",
        quant=None,
    ) -> Signal:
        """bias: 시장/섹터/뉴스 컨텍스트 점수 보정(-100~100). entry_block: 신규 진입 금지 사유. exit_hint: 보유 시 강제 청산 사유."""
        p = self.p
        r = ind.iloc[-1]
        prev = ind.iloc[-2] if len(ind) >= 2 else r
        ts = ind.index[-1].to_pydatetime() if hasattr(ind.index[-1], "to_pydatetime") else ind.index[-1]
        price = float(r.close)
        atr_v = float(r.atr) if not np.isnan(r.atr) else 0.0

        if len(ind) < self.min_bars or np.isnan(r.ema_slow) or np.isnan(r.rsi):
            return Signal(code, ts, Action.HOLD, 0.0, price, "unknown", ["데이터 부족"], {}, atr_v)

        adx_v = float(r.adx) if not np.isnan(r.adx) else 0.0
        trending = adx_v >= p.adx_trend
        regime = "trend" if trending else "range"
        idx = 0 if trending else 1

        t, why_t = self._trend(r, prev)
        m, why_m = self._momentum(r, prev)
        b, why_b = self._breakout(r, prev, ind)
        v, why_v = self._reversion(r, prev)
        st, why_st = self._supertrend(r, prev)

        weights = {
            "trend": p.w_trend[idx],
            "momentum": p.w_momentum[idx],
            "breakout": p.w_breakout[idx],
            "reversion": p.w_reversion[idx],
            "supertrend": p.w_supertrend[idx],
        }
        comps = {"trend": t, "momentum": m, "breakout": b, "reversion": v, "supertrend": st}
        reasons = why_t + why_m + why_b + why_v + why_st
        buy_threshold = p.buy_threshold
        if quant is not None:
            qw = {"xs_momentum": p.w_xs_momentum[idx], "tsmom": p.w_tsmom[idx], "pairs": p.w_pairs[idx], "micro": p.w_micro[idx], "alpha": p.w_alpha[idx]}
            for name, (val, why) in quant.components.items():
                if name in qw:
                    comps[name] = val
                    weights[name] = qw[name]
                    reasons.extend(why)
            if quant.vol_regime == "high":
                buy_threshold += p.high_vol_threshold_add
                reasons.append(f"고변동성 국면(x{quant.vol_ratio:.1f})")
        raw = sum(comps[k] * weights[k] for k in comps)
        wsum = sum(weights.values())
        score = raw / wsum * 100.0

        vr = float(r.vol_ratio) if not np.isnan(r.vol_ratio) else 1.0
        if vr >= 2.0:
            score *= 1.15
            reasons.append(f"거래량 급증(x{vr:.1f})")
        elif vr < p.min_vol_ratio:
            score *= 0.7
        if bias:
            score += bias
            reasons.append(f"컨텍스트 보정 {bias:+.0f}")
        score = _clip(score, -100.0, 100.0)
        comps["bias"] = float(bias)
        comps["vol_ratio"] = vr
        comps["adx"] = adx_v
        comps["rsi"] = float(r.rsi)

        action = Action.HOLD
        if not has_position and score >= buy_threshold:
            blocked = []
            if p.require_above_vwap and price < r.vwap:
                blocked.append("VWAP 아래")
            if r.rsi > p.max_rsi_entry:
                blocked.append("RSI 과열")
            if vr < p.min_vol_ratio:
                blocked.append("거래량 부족")
            if not np.isnan(r.bb_width) and r.bb_width < p.min_bb_width_pct:
                blocked.append("변동성 부족")
            if entry_block:
                blocked.append(entry_block)
            if blocked:
                reasons.append("진입 보류: " + ", ".join(blocked))
            else:
                action = Action.BUY
        elif has_position:
            exit_now = score <= p.sell_threshold
            if exit_hint:
                exit_now = True
                reasons.append(f"청산: {exit_hint}")
            if r.st_dir < 0 and prev.st_dir >= 0:
                exit_now = True
                reasons.append("청산: 슈퍼트렌드 하락 전환")
            if r.close < r.vwap and prev.close < prev.vwap and r.ema_fast < r.ema_slow:
                exit_now = True
                reasons.append("청산: VWAP 하회 + EMA 역배열")
            if exit_now:
                action = Action.SELL
        elif not has_position and score <= p.sell_threshold:
            # 미보유 종목의 약세 신호 (참고용, 공매도 미지원)
            action = Action.HOLD
        return Signal(code, ts, action, round(score, 1), price, regime, reasons, comps, atr_v)

    def evaluate(self, code: str, df: pd.DataFrame, has_position: bool = False) -> Signal:
        if len(df) == 0:
            from datetime import datetime

            return Signal(code, datetime.now(), Action.HOLD, 0.0, 0.0, "unknown", ["데이터 없음"], {}, 0.0)
        ind = compute_all(df, self.p.indicator_params())
        return self.evaluate_with_indicators(code, ind, has_position)
