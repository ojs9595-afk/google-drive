"""시세 데이터 기본 타입과 피드 인터페이스."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Iterable

import pandas as pd


@dataclass(slots=True)
class Tick:
    """체결 틱. volume 은 해당 체결 수량, acc_volume 은 당일 누적 거래량."""

    code: str
    ts: datetime
    price: float
    volume: int = 0
    acc_volume: int = 0
    change_pct: float = 0.0
    ask: float = 0.0
    bid: float = 0.0
    name: str = ""
    # 마이크로스트럭처 (KIS 실시간 체결 제공, 없으면 0)
    strength: float = 0.0  # 체결강도 (100 = 중립)
    buy_vol: int = 0  # 매수 체결량 누계
    sell_vol: int = 0  # 매도 체결량 누계
    ask_qty: int = 0  # 총 매도호가 잔량
    bid_qty: int = 0  # 총 매수호가 잔량


@dataclass(slots=True)
class Candle:
    ts: datetime  # 캔들 시작 시각
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_row(self) -> dict:
        return {
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def candles_to_df(candles: Iterable[Candle]) -> pd.DataFrame:
    cs = list(candles)
    if not cs:
        return pd.DataFrame({k: pd.Series(dtype=float) for k in ["open", "high", "low", "close", "volume"]})
    idx = pd.DatetimeIndex([c.ts for c in cs])
    return pd.DataFrame(
        {
            "open": [c.open for c in cs],
            "high": [c.high for c in cs],
            "low": [c.low for c in cs],
            "close": [c.close for c in cs],
            "volume": [c.volume for c in cs],
        },
        index=idx,
    )


TickCallback = Callable[[Tick], Awaitable[None] | None]


class HistoryProvider(abc.ABC):
    """과거 캔들 조회 인터페이스."""

    @abc.abstractmethod
    def minute_candles(self, code: str, count: int = 400, interval: int = 1) -> list[Candle]:
        ...

    def daily_candles(self, code: str, count: int = 60) -> list[Candle]:  # pragma: no cover - optional
        raise NotImplementedError


class DataFeed(HistoryProvider):
    """실시간 시세 피드 인터페이스."""

    name = "base"

    def __init__(self) -> None:
        self._codes: list[str] = []
        self._callback: TickCallback | None = None
        self._running = False

    def subscribe(self, codes: Iterable[str]) -> None:
        for c in codes:
            if c not in self._codes:
                self._codes.append(c)

    @property
    def codes(self) -> list[str]:
        return list(self._codes)

    def on_tick(self, cb: TickCallback) -> None:
        self._callback = cb

    async def _emit(self, tick: Tick) -> None:
        if self._callback is None:
            return
        res = self._callback(tick)
        if res is not None:
            await res

    @abc.abstractmethod
    async def run(self) -> None:
        """틱을 수신해 콜백으로 전달. stop() 호출 시 종료."""

    def stop(self) -> None:
        self._running = False
