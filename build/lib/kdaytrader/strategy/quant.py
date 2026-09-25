"""퀀트 하우스 공개 전략군을 데이트레이딩 시간축에 맞춰 옮긴 알파 모듈.

각 알파는 -1 ~ +1 점수와 근거 문자열을 내며, EnsembleStrategy 가 국면별 가중치로 합산한다.

| 구성요소       | 원류(공개된 전략 계열)                                             | 구현                                   |
|---------------|-------------------------------------------------------------------|----------------------------------------|
| pairs         | 통계적 차익거래·페어 트레이딩 (Renaissance, D.E. Shaw, PDT, Millennium, Two Sigma) | 롤링 OLS 헤지비율 + 스프레드 z-score + OU 반감기 |
| micro         | 마켓메이킹·마이크로스트럭처 (Jane Street, Citadel Securities, Optiver, IMC, SIG, Virtu) | 호가잔량 불균형(OBI), 체결흐름 불균형(TFI), 체결강도, 마이크로프라이스 |
| xs_momentum   | 횡단면 모멘텀·상대강도 팩터 (AQR, Two Sigma, Cubist, QRT)            | 유니버스 대비 수익률 z-score              |
| tsmom         | 시계열 모멘텀 / 추세추종 (AQR TSMOM, DRW, Jump 계열 CTA 접근)         | 다중 룩백 변동성 조정 수익률 부호           |
| alpha         | 공식형 알파 (WorldQuant "101 Formulaic Alphas")                     | Alpha#101, #12, #9, #53 변형의 랭크 결합    |
| vol_regime    | 변동성 국면·리스크 패리티 (AQR, Two Sigma, Citadel 리스크 그리드)      | 단기/장기 실현변동성 비율 → 사이징 스케일    |
| kelly (risk)  | 켈리 기준 자금관리 (Renaissance/Thorp 계보, PDT)                     | 최근 거래 승률·손익비 기반 분수 켈리 클램프   |

이들은 해당 회사의 실제 비공개 모델이 아니라, 학계·업계에 공개된 전략 원리의 단순화 구현이다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class QuantParams:
    enabled: bool = True
    xs_lookback: int = 30  # 횡단면 모멘텀 룩백(봉)
    xs_min_universe: int = 3
    pairs_window: int = 120
    pairs_entry_z: float = 2.0
    pairs_exit_z: float = 0.5
    pairs_max_half_life: float = 60.0  # 봉 단위
    pairs_min_corr: float = 0.5
    tsmom_lookbacks: tuple[int, ...] = (20, 60, 120)
    alpha_window: int = 60
    vol_short: int = 20
    vol_long: int = 120
    vol_high_ratio: float = 1.5
    vol_low_ratio: float = 0.7
    micro_ewma_alpha: float = 0.2


@dataclass
class MicroState:
    """틱 단위로 갱신되는 마이크로스트럭처 상태."""

    obi: float = 0.0  # (bid_qty - ask_qty)/(bid_qty + ask_qty) EWMA
    tfi: float = 0.0  # 체결흐름 불균형 EWMA
    strength: float = 100.0  # 체결강도 EWMA
    micro_dev: float = 0.0  # (마이크로프라이스 - 중간가)/호가단위
    last_buy: int = 0
    last_sell: int = 0
    samples: int = 0

    def update(self, tick, alpha: float) -> None:
        if tick.ask_qty > 0 and tick.bid_qty > 0:
            obi = (tick.bid_qty - tick.ask_qty) / (tick.bid_qty + tick.ask_qty)
            self.obi = obi if self.samples == 0 else (1 - alpha) * self.obi + alpha * obi
            if tick.ask > 0 and tick.bid > 0:
                mid = (tick.ask + tick.bid) / 2.0
                microprice = (tick.ask * tick.bid_qty + tick.bid * tick.ask_qty) / (tick.bid_qty + tick.ask_qty)
                spread = max(tick.ask - tick.bid, 1e-9)
                dev = (microprice - mid) / spread
                self.micro_dev = dev if self.samples == 0 else (1 - alpha) * self.micro_dev + alpha * dev
        if tick.buy_vol > 0 or tick.sell_vol > 0:
            db = max(0, tick.buy_vol - self.last_buy)
            ds = max(0, tick.sell_vol - self.last_sell)
            self.last_buy, self.last_sell = tick.buy_vol, tick.sell_vol
            if db + ds > 0:
                tfi = (db - ds) / (db + ds)
                self.tfi = tfi if self.samples == 0 else (1 - alpha) * self.tfi + alpha * tfi
        if tick.strength > 0:
            self.strength = tick.strength if self.samples == 0 else (1 - alpha) * self.strength + alpha * tick.strength
        self.samples += 1

    @property
    def available(self) -> bool:
        return self.samples >= 3


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    if x != x:  # NaN
        return 0.0
    return float(max(lo, min(hi, x)))


def _rank_pct(series: pd.Series) -> float:
    """마지막 값의 윈도우 내 백분위 랭크를 -1~1 로."""
    s = series.dropna()
    if len(s) < 5:
        return 0.0
    r = (s.rank(pct=True).iloc[-1] - 0.5) * 2.0
    return _clip(r)


# ---------------------------------------------------------------------------
def pairs_signal(y_close: pd.Series, x_close: pd.Series, p: QuantParams) -> tuple[float, float, float, float]:
    """스프레드 z-score, 헤지비율, OU 반감기, 상관계수. 데이터 부족 시 (0, nan, nan, nan)."""
    n = min(len(y_close), len(x_close), p.pairs_window)
    if n < 40:
        return 0.0, float("nan"), float("nan"), float("nan")
    y = np.log(y_close.to_numpy(dtype=float)[-n:])
    x = np.log(x_close.to_numpy(dtype=float)[-n:])
    corr = float(np.corrcoef(np.diff(y), np.diff(x))[0, 1]) if n > 2 else 0.0
    xm, ym = x.mean(), y.mean()
    var = ((x - xm) ** 2).sum()
    beta = ((x - xm) * (y - ym)).sum() / var if var > 0 else 0.0
    spread = y - beta * x
    sd = spread.std()
    if sd <= 1e-12:
        return 0.0, beta, float("nan"), corr
    z = (spread[-1] - spread.mean()) / sd
    # OU 반감기: Δs_t = λ s_{t-1} + ε
    s_lag = spread[:-1] - spread.mean()
    ds = np.diff(spread)
    denom = (s_lag ** 2).sum()
    lam = (s_lag * ds).sum() / denom if denom > 0 else 0.0
    half_life = -math.log(2) / lam if lam < 0 else float("inf")
    return float(z), float(beta), float(half_life), corr


def tsmom_signal(close: pd.Series, atr: pd.Series, lookbacks: tuple[int, ...]) -> float:
    c = close.to_numpy(dtype=float)
    a = float(atr.iloc[-1]) if len(atr) and atr.iloc[-1] == atr.iloc[-1] else 0.0
    if a <= 0 or len(c) < 2:
        return 0.0
    vals = []
    for L in lookbacks:
        if len(c) <= L:
            continue
        ret = c[-1] - c[-1 - L]
        vals.append(_clip(ret / (a * math.sqrt(L) * 1.5)))
    return float(np.mean(vals)) if vals else 0.0


def formulaic_alphas(df: pd.DataFrame, window: int) -> tuple[float, dict[str, float]]:
    """WorldQuant 101 스타일 공식형 알파 (일중 봉에 맞게 변형)."""
    d = df.iloc[-(window + 12):]
    if len(d) < 20:
        return 0.0, {}
    o, h, l, c, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
    eps = 1e-9
    # Alpha#101: (close - open) / ((high - low) + .001)  → 캔들 몸통 방향성
    a101 = (c - o) / ((h - l) + eps)
    # Alpha#12: sign(delta(volume,1)) * (-1 * delta(close,1)) → 거래량 증가 시 단기 역추세
    a12 = np.sign(v.diff()) * (-c.diff())
    # Alpha#9 변형: 5봉 연속 상승/하락이면 추세 추종, 아니면 역추세
    dc = c.diff()
    mn = dc.rolling(5).min()
    mx = dc.rolling(5).max()
    a9 = pd.Series(np.where(mn > 0, dc, np.where(mx < 0, dc, -dc)), index=c.index)
    # Alpha#53 변형: -delta(((close-low)-(high-close))/(close-low), 9) → 캔들 내 위치 변화
    pos = ((c - l) - (h - c)) / ((c - l) + eps)
    a53 = -pos.diff(9)
    scores = {
        "a101": _rank_pct(a101.iloc[-window:]),
        "a12": _rank_pct(a12.iloc[-window:]),
        "a9": _rank_pct(a9.iloc[-window:]),
        "a53": _rank_pct(a53.iloc[-window:]),
    }
    weights = {"a101": 0.3, "a12": 0.2, "a9": 0.3, "a53": 0.2}
    total = sum(scores[k] * weights[k] for k in scores)
    return _clip(total), scores


def vol_regime(close: pd.Series, short: int, long: int, hi: float, lo: float) -> tuple[str, float, float]:
    """(국면, 단기/장기 변동성 비율, 사이징 스케일)."""
    r = np.log(close).diff().dropna()
    if len(r) < long:
        return "normal", 1.0, 1.0
    rs = r.iloc[-short:].std()
    rl = r.iloc[-long:].std()
    if rl <= 0 or rs != rs:
        return "normal", 1.0, 1.0
    ratio = float(rs / rl)
    regime = "high" if ratio >= hi else ("low" if ratio <= lo else "normal")
    scale = _clip(1.0 / ratio, 0.5, 1.5)  # 변동성 타게팅: 변동성 커지면 사이즈 축소
    return regime, ratio, scale


# ---------------------------------------------------------------------------
@dataclass
class QuantSnapshot:
    components: dict[str, tuple[float, list[str]]] = field(default_factory=dict)
    vol_regime: str = "normal"
    vol_ratio: float = 1.0
    vol_scale: float = 1.0
    pairs_z: float = float("nan")
    partner: str = ""


class QuantSignals:
    """캔들 저장소와 유니버스 상태를 참조해 종목별 퀀트 알파를 계산한다."""

    def __init__(self, store, params: QuantParams | None = None, pairs: dict[str, str] | None = None, sectors: dict[str, str] | None = None):
        self.store = store
        self.p = params or QuantParams()
        self.pairs = dict(pairs or {})
        self.sectors = dict(sectors or {})
        self.micro: dict[str, MicroState] = {}
        self.universe_ret: dict[str, float] = {}
        self.last: dict[str, QuantSnapshot] = {}

    # ----- 틱 -----
    def on_tick(self, tick) -> None:
        self.micro.setdefault(tick.code, MicroState()).update(tick, self.p.micro_ewma_alpha)

    # ----- 유틸 -----
    def partner_of(self, code: str) -> str:
        if code in self.pairs:
            return self.pairs[code]
        sec = self.sectors.get(code)
        if not sec:
            return ""
        for other in self.store.codes():
            if other != code and self.sectors.get(other) == sec:
                return other
        return ""

    def update_universe(self, code: str, close: pd.Series) -> None:
        L = self.p.xs_lookback
        if len(close) > L and close.iloc[-1 - L] > 0:
            self.universe_ret[code] = float(close.iloc[-1] / close.iloc[-1 - L] - 1.0)

    # ----- 계산 -----
    def compute(self, code: str, ind: pd.DataFrame, has_position: bool = False) -> QuantSnapshot:
        snap = QuantSnapshot()
        if not self.p.enabled or len(ind) < 20:
            return snap
        p = self.p
        close = ind["close"]
        self.update_universe(code, close)

        # 1) 횡단면 모멘텀 (AQR / Two Sigma 식 상대강도)
        if len(self.universe_ret) >= p.xs_min_universe and code in self.universe_ret:
            vals = np.array(list(self.universe_ret.values()))
            sd = vals.std()
            if sd > 1e-9:
                z = (self.universe_ret[code] - vals.mean()) / sd
                why = []
                if z >= 1.0:
                    why.append(f"횡단면 모멘텀 상위(z={z:+.1f})")
                elif z <= -1.0:
                    why.append(f"횡단면 모멘텀 하위(z={z:+.1f})")
                snap.components["xs_momentum"] = (_clip(z / 2.0), why)

        # 2) 시계열 모멘텀 (AQR TSMOM)
        ts = tsmom_signal(close, ind["atr"], p.tsmom_lookbacks)
        snap.components["tsmom"] = (ts, [f"시계열 모멘텀 {ts:+.2f}"] if abs(ts) >= 0.6 else [])

        # 3) 페어 스프레드 (통계적 차익거래)
        partner = self.partner_of(code)
        if partner:
            pdf = self.store.df(partner, n=p.pairs_window + 5)
            if len(pdf) >= 40:
                y = close.iloc[-p.pairs_window:]
                x = pdf["close"]
                # 시각 정렬
                joined = pd.concat([y.rename("y"), x.rename("x")], axis=1, join="inner").dropna()
                if len(joined) >= 40:
                    z, beta, hl, corr = pairs_signal(joined["y"], joined["x"], p)
                    snap.pairs_z, snap.partner = z, partner
                    if corr >= p.pairs_min_corr and 1.0 <= hl <= p.pairs_max_half_life:
                        why = []
                        if z <= -p.pairs_entry_z:
                            s = 1.0
                            why.append(f"페어 저평가 z={z:+.1f} vs {partner} (반감기 {hl:.0f}봉)")
                        elif z >= p.pairs_entry_z:
                            s = -1.0
                            why.append(f"페어 고평가 z={z:+.1f} vs {partner}")
                        elif has_position and abs(z) <= p.pairs_exit_z:
                            s = 0.0
                        else:
                            s = _clip(-z / p.pairs_entry_z * 0.5)
                        snap.components["pairs"] = (s, why)

        # 4) 마이크로스트럭처 (마켓메이커 시그널)
        m = self.micro.get(code)
        if m is not None and m.available:
            strength_s = _clip((m.strength - 100.0) / 50.0)
            s = 0.35 * _clip(m.obi * 2.0) + 0.35 * _clip(m.tfi * 2.0) + 0.2 * strength_s + 0.1 * _clip(m.micro_dev * 4.0)
            why = []
            if s >= 0.5:
                why.append(f"호가/체결 매수 우위(OBI {m.obi:+.2f}, 체결강도 {m.strength:.0f})")
            elif s <= -0.5:
                why.append(f"호가/체결 매도 우위(OBI {m.obi:+.2f}, 체결강도 {m.strength:.0f})")
            snap.components["micro"] = (_clip(s), why)

        # 5) 공식형 알파
        a, parts = formulaic_alphas(ind, p.alpha_window)
        snap.components["alpha"] = (a, [f"공식형 알파 {a:+.2f}"] if abs(a) >= 0.5 else [])

        # 6) 변동성 국면
        snap.vol_regime, snap.vol_ratio, snap.vol_scale = vol_regime(close, p.vol_short, p.vol_long, p.vol_high_ratio, p.vol_low_ratio)
        self.last[code] = snap
        return snap
