"""브로커(주문 집행) 인터페이스와 포지션/체결 타입."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Fill:
    code: str
    side: str  # BUY / SELL
    qty: int
    price: float
    ts: datetime
    fee: float = 0.0
    order_id: str = ""


@dataclass
class Position:
    code: str
    qty: int
    avg_price: float
    entry_ts: datetime
    stop_price: float = 0.0
    take_profit: float = 0.0
    highest: float = 0.0
    atr_at_entry: float = 0.0
    risk_per_share: float = 0.0
    breakeven_moved: bool = False
    partial_done: bool = False
    name: str = ""
    entry_reason: str = ""

    def unrealized(self, price: float) -> float:
        return (price - self.avg_price) * self.qty

    def unrealized_pct(self, price: float) -> float:
        if self.avg_price == 0:
            return 0.0
        return (price / self.avg_price - 1.0) * 100.0

    def r_multiple(self, price: float) -> float:
        if self.risk_per_share <= 0:
            return 0.0
        return (price - self.avg_price) / self.risk_per_share


@dataclass
class Trade:
    """청산 완료된 거래 기록."""

    code: str
    qty: int
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    pnl: float  # 비용 차감 후 손익
    pnl_pct: float
    fees: float
    reason: str = ""
    name: str = ""

    @property
    def holding_minutes(self) -> float:
        return (self.exit_ts - self.entry_ts).total_seconds() / 60.0


class Broker(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def cash(self) -> float:
        ...

    @abc.abstractmethod
    def positions(self) -> dict[str, Position]:
        ...

    @abc.abstractmethod
    def buy(self, code: str, qty: int, price: float, ts: datetime, reason: str = "", name: str = "") -> Fill | None:
        ...

    @abc.abstractmethod
    def sell(self, code: str, qty: int, price: float, ts: datetime, reason: str = "") -> Fill | None:
        ...

    @abc.abstractmethod
    def trades(self) -> list[Trade]:
        ...

    def position(self, code: str) -> Position | None:
        return self.positions().get(code)

    def equity(self, prices: dict[str, float]) -> float:
        total = self.cash()
        for code, pos in self.positions().items():
            total += pos.qty * prices.get(code, pos.avg_price)
        return total

    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.trades())
