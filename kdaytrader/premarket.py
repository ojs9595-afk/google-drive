"""장전 자동 종목 선정(Pre-market universe selection)과 장중 재선정.

매 거래일 개장 전(기본 08:50) 후보군을 모아 일봉 기반 '데이트레이딩 적합도' 점수를 계산하고 상위 N개를 그날의 관심종목으로 삼는다.
후보군: 거래량 상위, 시가총액 상위, 전일 상승률 상위(코스피·코스닥) + 관심 고정 종목. 시뮬레이션 모드는 합성 일봉으로 동작한다.

점수(0~100) 구성
- 유동성(25): 20일 평균 거래대금 (30억 미만 제외, 500억 이상 만점)
- 변동성(20): 일간 ATR% 2~6% 구간이 최적, 10% 초과 제외
- 모멘텀(20): 5일 수익률, 20일선 위, 일봉 RSI 45~70
- 거래량 급증(15): 전일 거래량 / 20일 평균
- 마감 강도(10): 전일 종가의 일중 위치
- 뉴스·섹터(10): 컨텍스트의 섹터/종목 감성 (종목 악재 -0.45 이하 제외)
"""
from __future__ import annotations

import json
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .market import KST, now_kst

log = logging.getLogger(__name__)


@dataclass
class PremarketParams:
    enabled: bool = True
    time: str = "08:50"  # 선정 시각 (KST)
    size: int = 30  # 선정 종목 수
    mode: str = "replace"  # replace: 관심종목 = 고정 + 선정 / merge: 기존에 추가
    pinned: list[str] = field(default_factory=list)  # 항상 포함할 종목코드
    min_price: float = 2_000
    max_price: float = 500_000
    min_turnover: float = 3e9  # 20일 평균 거래대금(원)
    max_atr_pct: float = 10.0
    candidate_limit: int = 220  # 후보 최대 수 (일봉 조회 건수)
    intraday_refresh_min: int = 30  # 장중 재선정 주기 (0=끔)
    intraday_add: int = 5  # 장중 재선정 시 추가 편입 최대 수
    max_universe: int = 60
    auto_start: bool = False  # 선정 후 개장 시 엔진 자동 시작
    auto_start_feed: str = "naver"
    auto_start_signal_only: bool = True
    run_on_engine_start: bool = True  # 오늘 선정이 없으면 엔진 시작 시 먼저 선정


@dataclass
class Pick:
    code: str
    name: str
    score: float
    reasons: list[str]
    metrics: dict
    sources: list[str]


@dataclass
class Selection:
    date: str
    ts: str
    picks: list[Pick]
    candidates: int
    scored: int
    excluded: int
    sources: dict
    errors: list[str]
    mode: str
    feed: str

    def to_dict(self) -> dict:
        return {"date": self.date, "ts": self.ts, "picks": [asdict(p) for p in self.picks], "candidates": self.candidates, "scored": self.scored, "excluded": self.excluded, "sources": self.sources, "errors": self.errors, "mode": self.mode, "feed": self.feed}

    @staticmethod
    def from_dict(d: dict) -> "Selection":
        return Selection(d["date"], d["ts"], [Pick(**p) for p in d.get("picks", [])], d.get("candidates", 0), d.get("scored", 0), d.get("excluded", 0), d.get("sources", {}), d.get("errors", []), d.get("mode", "replace"), d.get("feed", ""))


def _tri(x: float, lo: float, peak_lo: float, peak_hi: float, hi: float) -> float:
    """사다리꼴 점수 0~1: [peak_lo, peak_hi] 에서 1, lo/hi 밖에서 0."""
    if x <= lo or x >= hi:
        return 0.0
    if x < peak_lo:
        return (x - lo) / (peak_lo - lo)
    if x > peak_hi:
        return (hi - x) / (hi - peak_hi)
    return 1.0


def daily_metrics(df: pd.DataFrame) -> dict | None:
    """일봉 DataFrame(open/high/low/close/volume, 오름차순) → 지표. 20일 미만이면 None."""
    if df is None or len(df) < 21:
        return None
    d = df.iloc[-60:]
    c, h, l, v, o = d["close"], d["high"], d["low"], d["volume"], d["open"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    last = float(c.iloc[-1])
    if last <= 0:
        return None
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    rsi = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    vol20 = float(v.iloc[-21:-1].mean()) if len(v) > 21 else float(v.iloc[:-1].mean())
    rng = float(h.iloc[-1] - l.iloc[-1])
    return {
        "price": last,
        "turnover20": float((c * v).iloc[-20:].mean()),
        "atr_pct": float(atr / last * 100),
        "ret5": float(last / c.iloc[-6] - 1) * 100 if len(c) > 6 else 0.0,
        "ret20": float(last / c.iloc[-21] - 1) * 100 if len(c) > 21 else 0.0,
        "above_ma20": bool(last > c.iloc[-20:].mean()),
        "rsi": float(rsi),
        "vol_ratio": float(v.iloc[-1] / vol20) if vol20 > 0 else 1.0,
        "close_pos": float((c.iloc[-1] - l.iloc[-1]) / rng) if rng > 0 else 0.5,
        "chg1": float(last / prev_c.iloc[-1] - 1) * 100 if prev_c.iloc[-1] > 0 else 0.0,
    }


def score_stock(m: dict, p: PremarketParams, sector_sent: float = 0.0, stock_sent: float = 0.0) -> tuple[float, list[str], str]:
    """(점수, 근거, 제외사유). 제외사유가 비어 있지 않으면 후보에서 뺀다."""
    if not (p.min_price <= m["price"] <= p.max_price):
        return 0.0, [], f"가격 범위 밖({m['price']:,.0f})"
    if m["turnover20"] < p.min_turnover:
        return 0.0, [], f"거래대금 부족({m['turnover20'] / 1e8:.0f}억)"
    if m["atr_pct"] > p.max_atr_pct:
        return 0.0, [], f"변동성 과다(ATR {m['atr_pct']:.1f}%)"
    if stock_sent <= -0.45:
        return 0.0, [], f"종목 악재 뉴스({stock_sent:+.2f})"
    if m["chg1"] >= 25 or m["chg1"] <= -20:
        return 0.0, [], f"전일 급변({m['chg1']:+.1f}%)"
    reasons = []
    # 유동성 25
    liq = min(1.0, max(0.0, (math.log10(m["turnover20"]) - math.log10(p.min_turnover)) / (math.log10(5e10) - math.log10(p.min_turnover))))
    s = liq * 25
    reasons.append(f"거래대금 {m['turnover20'] / 1e8:,.0f}억")
    # 변동성 20
    vs = _tri(m["atr_pct"], 0.8, 2.0, 6.0, p.max_atr_pct)
    s += vs * 20
    reasons.append(f"ATR {m['atr_pct']:.1f}%")
    # 모멘텀 20
    mom = _tri(m["ret5"], -6.0, 1.0, 12.0, 25.0) * 8 + (8 if m["above_ma20"] else 0) + _tri(m["rsi"], 30, 45, 70, 85) * 4
    s += mom
    if m["above_ma20"]:
        reasons.append("20일선 위")
    reasons.append(f"5일 {m['ret5']:+.1f}%")
    # 거래량 급증 15
    vr = min(1.0, max(0.0, (m["vol_ratio"] - 1.0) / 3.0))
    s += vr * 15
    if m["vol_ratio"] >= 1.5:
        reasons.append(f"거래량 {m['vol_ratio']:.1f}배")
    # 마감 강도 10
    s += max(0.0, min(1.0, (m["close_pos"] - 0.3) / 0.6)) * 10
    if m["close_pos"] >= 0.7:
        reasons.append("강한 마감")
    # 뉴스·섹터 10
    ns = max(-1.0, min(1.0, sector_sent * 0.6 + stock_sent * 0.8))
    s += (ns + 1) / 2 * 10
    if abs(sector_sent) >= 0.15:
        reasons.append(f"섹터 뉴스 {sector_sent:+.2f}")
    if abs(stock_sent) >= 0.15:
        reasons.append(f"종목 뉴스 {stock_sent:+.2f}")
    return round(min(100.0, s), 1), reasons, ""


class UniverseSelector:
    def __init__(self, cfg: dict, params: PremarketParams, context=None, kis_client=None, feed_name: str = "naver", log_dir: str = "logs"):
        self.cfg = cfg
        self.p = params
        self.ctx = context
        self.kis = kis_client
        self.feed_name = feed_name
        self.log_dir = Path(log_dir)

    # ----- 후보군 -----
    def candidates(self) -> tuple[dict[str, str], dict[str, list[str]], dict, list[str]]:
        """{코드: 이름}, {코드: [출처]}, 출처별 건수, 오류."""
        from .screener import is_fund_like

        names: dict[str, str] = {}
        srcs: dict[str, list[str]] = {}
        counts: dict = {}
        errors: list[str] = []

        def add(code: str, name: str, src: str):
            if is_fund_like(name) or name.endswith(("우", "우B", "우C")) or not code.isdigit():
                return
            names.setdefault(code, name)
            srcs.setdefault(code, []).append(src)
            counts[src] = counts.get(src, 0) + 1

        wl = self.cfg.get("watchlist") or {}
        for c in self.p.pinned:
            add(str(c).zfill(6), wl.get(str(c).zfill(6), ""), "고정")
        if self.feed_name == "sim":
            from .config import DEFAULT_WATCHLIST

            for c, n in {**DEFAULT_WATCHLIST, **wl}.items():
                add(c, n, "시뮬레이션")
            return names, srcs, counts, errors
        from .data.naver import top_marketcap_codes, top_rise_codes, top_volume_codes

        jobs = []
        for m in ("KOSPI", "KOSDAQ"):
            jobs.append(("거래량상위", m, lambda m=m: top_volume_codes(80, m)))
            jobs.append(("상승률상위", m, lambda m=m: top_rise_codes(60, m)))
            jobs.append(("시총상위", m, lambda m=m: top_marketcap_codes(60 if m == "KOSPI" else 40, m)))
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [(src, m, ex.submit(fn)) for src, m, fn in jobs]
            for src, m, f in futs:
                try:
                    for code, name in f.result(timeout=30):
                        add(code, name, f"{src}({'코스피' if m == 'KOSPI' else '코스닥'})")
                except Exception as e:
                    errors.append(f"{src} {m}: {str(e)[:80]}")
        for c, n in wl.items():  # 기존 관심종목도 후보에 포함 (점수로 경쟁)
            add(c, n, "기존")
        return names, srcs, counts, errors

    # ----- 일봉 -----
    def daily_df(self, code: str) -> pd.DataFrame | None:
        if self.feed_name == "sim":
            from .data.base import candles_to_df
            from .data.sim import DEFAULT_BASE_PRICES, generate_history

            seed = int(now_kst().strftime("%Y%m%d")) + int(code)  # 날마다 다른 합성 데이터
            mins = candles_to_df(generate_history(code, 30, DEFAULT_BASE_PRICES.get(code, 50_000), seed=seed))
            d = mins.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
            return d
        if self.kis is not None:
            try:
                from .data.base import candles_to_df

                return candles_to_df(self.kis.daily_candles(code, 60))
            except Exception:
                pass
        from .data.base import candles_to_df
        from .data.naver import NaverFeed

        return candles_to_df(NaverFeed().daily_candles(code, 60))

    # ----- 선정 -----
    def select(self, size: int | None = None) -> Selection:
        size = size or self.p.size
        names, srcs, counts, errors = self.candidates()
        codes = list(names)[: self.p.candidate_limit]
        now = now_kst()
        picks: list[Pick] = []
        excluded = 0
        scored = 0

        def work(code: str):
            try:
                df = self.daily_df(code)
                m = daily_metrics(df)
                if m is None:
                    return code, None, "일봉 부족"
                sector = self.ctx.sector_of(code) if self.ctx is not None else ""
                sec_s = self.ctx.news_sentiment(now, sector=sector)[0] if (self.ctx is not None and sector) else 0.0
                st_s = self.ctx.news_sentiment(now, code=code)[0] if self.ctx is not None else 0.0
                s, reasons, why_not = score_stock(m, self.p, sec_s, st_s)
                return code, (s, reasons, m), why_not
            except Exception as e:
                return code, None, f"오류: {str(e)[:60]}"

        with ThreadPoolExecutor(max_workers=8 if self.feed_name != "sim" else 4) as ex:
            for code, res, why_not in ex.map(work, codes):
                if res is None:
                    excluded += 1
                    if len(errors) < 30 and not why_not.startswith("일봉"):
                        errors.append(f"{code}: {why_not}")
                    continue
                scored += 1
                s, reasons, m = res
                if why_not:
                    excluded += 1
                    continue
                picks.append(Pick(code, names.get(code) or code, s, reasons, {k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()}, srcs.get(code, [])))
        picks.sort(key=lambda x: -x.score)
        pinned = [str(c).zfill(6) for c in self.p.pinned]
        chosen = [p for p in picks if p.code in pinned] + [p for p in picks if p.code not in pinned][: max(0, size - len(pinned))]
        sel = Selection(now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), chosen, len(codes), scored, excluded, counts, errors[:20], self.p.mode, self.feed_name)
        self.save(sel)
        return sel

    # ----- 저장/복원 -----
    def path_for(self, d: date | None = None) -> Path:
        d = d or now_kst().date()
        return self.log_dir / f"universe_{d:%Y%m%d}.json"

    def save(self, sel: Selection) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.path_for(datetime.strptime(sel.date, "%Y-%m-%d").date()).write_text(json.dumps(sel.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log.debug("선정 결과 저장 실패: %s", e)

    def load_today(self) -> Selection | None:
        p = self.path_for()
        if not p.exists():
            return None
        try:
            return Selection.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return None

    # ----- 장중 재선정 -----
    def intraday_candidates(self, exclude: set[str], limit: int) -> dict[str, str]:
        """장중: 당일 거래량·상승률 상위에서 아직 없는 종목을 최대 limit 개."""
        if self.feed_name == "sim":
            return {}
        from .data.naver import top_rise_codes, top_volume_codes
        from .screener import is_fund_like

        out: dict[str, str] = {}
        for fn in (top_volume_codes, top_rise_codes):
            for m in ("KOSPI", "KOSDAQ"):
                try:
                    for code, name in fn(40, m):
                        if code in exclude or code in out or is_fund_like(name) or name.endswith(("우", "우B")):
                            continue
                        out[code] = name
                        if len(out) >= limit:
                            return out
                except Exception as e:
                    log.debug("장중 후보 조회 실패 %s: %s", m, e)
        return out


def should_run_premarket(now: datetime, params: PremarketParams, last_run_date: date | None) -> bool:
    """선정 시각 이후 ~ 09:30 사이에 오늘 아직 안 돌렸으면 True (주말 제외)."""
    if not params.enabled or now.weekday() >= 5:
        return False
    if last_run_date == now.date():
        return False
    try:
        h, m = str(params.time).split(":")
        t0 = time(int(h), int(m))
    except ValueError:
        t0 = time(8, 50)
    return t0 <= now.time() < time(9, 30)


def next_run_text(params: PremarketParams, now: datetime | None = None) -> str:
    now = now or now_kst()
    try:
        h, m = str(params.time).split(":")
        t0 = time(int(h), int(m))
    except ValueError:
        t0 = time(8, 50)
    cand = now.replace(hour=t0.hour, minute=t0.minute, second=0, microsecond=0)
    if now >= cand:
        cand += timedelta(days=1)
    while cand.weekday() >= 5:
        cand += timedelta(days=1)
    return cand.strftime("%m/%d %H:%M")
