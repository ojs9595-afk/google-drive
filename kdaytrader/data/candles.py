"""틱 → 분봉 집계 및 종목별 캔들 저장소."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

import pandas as pd

from .base import Candle, Tick, candles_to_df


def floor_ts(ts: datetime, interval_sec: int) -> datetime:
    base = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (ts - base).total_seconds()
    return base + timedelta(seconds=int(elapsed // interval_sec) * interval_sec)


class CandleBuilder:
    """단일 종목 틱을 지정 간격 캔들로 집계. 캔들이 닫힐 때 반환한다."""

    def __init__(self, interval_sec: int = 60, maxlen: int = 2000):
        self.interval_sec = interval_sec
        self.candles: deque[Candle] = deque(maxlen=maxlen)
        self.current: Candle | None = None
        self._last_acc_volume: int | None = None
        self._df_cache: pd.DataFrame | None = None

    def seed(self, candles: list[Candle]) -> None:
        """과거 캔들로 초기화. 마지막 캔들은 '진행 중'으로 간주하지 않고 모두 닫힌 것으로 본다."""
        self.candles.clear()
        for c in candles:
            self.candles.append(c)
        self.current = None
        self._df_cache = None

    def add_tick(self, tick: Tick) -> Candle | None:
        """틱을 반영하고, 새로운 캔들 구간이 시작되면 직전 캔들을 닫아 반환."""
        bucket = floor_ts(tick.ts, self.interval_sec)
        vol = tick.volume
        if vol <= 0 and tick.acc_volume > 0:
            # 누적 거래량만 오는 피드(예: 네이버 폴링)는 차분으로 체결량을 추정
            if self._last_acc_volume is not None and tick.acc_volume >= self._last_acc_volume:
                vol = tick.acc_volume - self._last_acc_volume
            else:
                vol = 0
        if tick.acc_volume > 0:
            self._last_acc_volume = tick.acc_volume

        closed: Candle | None = None
        if self.current is None:
            if self.candles:
                last = self.candles[-1]
                if bucket < last.ts:
                    return None  # 이미 시드된 과거 구간의 늦은 틱
                if bucket == last.ts:
                    # 과거 캔들 제공자가 진행 중인 봉을 포함해 준 경우: 그 봉을 이어서 갱신
                    self.candles.pop()
                    self._df_cache = None
                    self.current = last
                    last.high = max(last.high, tick.price)
                    last.low = min(last.low, tick.price)
                    last.close = tick.price
                    return None
            self.current = Candle(bucket, tick.price, tick.price, tick.price, tick.price, vol)
            return None
        if bucket > self.current.ts:
            closed = self.current
            self.candles.append(closed)
            self._df_cache = None
            self.current = Candle(bucket, tick.price, tick.price, tick.price, tick.price, vol)
            return closed
        if bucket < self.current.ts:
            # 늦게 도착한 틱은 무시
            return None
        c = self.current
        c.high = max(c.high, tick.price)
        c.low = min(c.low, tick.price)
        c.close = tick.price
        c.volume += vol
        return None

    def add_candle(self, candle: Candle) -> None:
        """이미 닫힌 캔들을 직접 추가 (백테스트용)."""
        self.candles.append(candle)
        self._df_cache = None

    def force_close(self) -> Candle | None:
        if self.current is None:
            return None
        closed = self.current
        self.candles.append(closed)
        self.current = None
        self._df_cache = None
        return closed

    def df(self, include_current: bool = False, n: int | None = None) -> pd.DataFrame:
        """최근 n개 캔들의 DataFrame. n=None 이면 전체."""
        if include_current and self.current is not None:
            cs = list(self.candles) + [self.current]
            return candles_to_df(cs[-n:] if n else cs)
        if n:
            if self._df_cache is not None and len(self._df_cache) == len(self.candles):
                return self._df_cache.iloc[-n:]
            cs = list(self.candles)
            return candles_to_df(cs[-n:])
        if self._df_cache is None:
            self._df_cache = candles_to_df(self.candles)
        return self._df_cache

    def __len__(self) -> int:
        return len(self.candles)


class CandleStore:
    """여러 종목의 CandleBuilder 를 관리."""

    def __init__(self, interval_sec: int = 60, maxlen: int = 2000):
        self.interval_sec = interval_sec
        self.maxlen = maxlen
        self._builders: dict[str, CandleBuilder] = {}
        self.last_price: dict[str, float] = {}
        self.last_tick: dict[str, Tick] = {}

    def builder(self, code: str) -> CandleBuilder:
        if code not in self._builders:
            self._builders[code] = CandleBuilder(self.interval_sec, self.maxlen)
        return self._builders[code]

    def seed(self, code: str, candles: list[Candle]) -> None:
        self.builder(code).seed(candles)
        if candles:
            self.last_price[code] = candles[-1].close

    def add_tick(self, tick: Tick) -> Candle | None:
        self.last_price[tick.code] = tick.price
        self.last_tick[tick.code] = tick
        return self.builder(tick.code).add_tick(tick)

    def add_candle(self, code: str, candle: Candle) -> None:
        self.last_price[code] = candle.close
        self.builder(code).add_candle(candle)

    def df(self, code: str, include_current: bool = False, n: int | None = None) -> pd.DataFrame:
        return self.builder(code).df(include_current, n)

    def codes(self) -> list[str]:
        return list(self._builders.keys())
