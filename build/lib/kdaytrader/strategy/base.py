"""전략 인터페이스와 시그널 타입."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import pandas as pd


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(slots=True)
class Signal:
    code: str
    ts: datetime
    action: Action
    score: float  # -100 ~ +100 (양수: 매수 우위, 음수: 매도 우위)
    price: float
    regime: str = "unknown"  # trend | range | unknown
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    atr: float = 0.0

    @property
    def strength(self) -> str:
        a = abs(self.score)
        if a >= 80:
            return "매우 강함"
        if a >= 60:
            return "강함"
        if a >= 40:
            return "보통"
        return "약함"


class Strategy(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def evaluate(self, code: str, df: pd.DataFrame, has_position: bool = False) -> Signal:
        """지표가 포함되지 않은 OHLCV DataFrame 을 받아 마지막 캔들 기준 시그널을 낸다."""

    @property
    @abc.abstractmethod
    def min_bars(self) -> int:
        """시그널 계산에 필요한 최소 캔들 수."""
