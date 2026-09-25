"""모의(페이퍼) 브로커: 슬리피지와 수수료/세금을 반영해 즉시 체결."""
from __future__ import annotations

from datetime import datetime

from ..market import FeeModel, round_to_tick, tick_size
from .base import Broker, Fill, Position, Trade


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, initial_cash: float = 10_000_000, fee: FeeModel | None = None, slippage_ticks: int = 1):
        self.initial_cash = float(initial_cash)
        self._cash = float(initial_cash)
        self._positions: dict[str, Position] = {}
        self._trades: list[Trade] = []
        self._fills: list[Fill] = []
        self.fee = fee or FeeModel()
        self.slippage_ticks = slippage_ticks
        self._seq = 0

    def cash(self) -> float:
        return self._cash

    def positions(self) -> dict[str, Position]:
        return self._positions

    def trades(self) -> list[Trade]:
        return self._trades

    def fills(self) -> list[Fill]:
        return self._fills

    def _fill_price(self, price: float, side: str) -> float:
        if self.slippage_ticks <= 0:
            return round_to_tick(price)
        ts = tick_size(price)
        if side == "BUY":
            return round_to_tick(price + ts * self.slippage_ticks, "up")
        return round_to_tick(price - ts * self.slippage_ticks, "down")

    def buy(self, code: str, qty: int, price: float, ts: datetime, reason: str = "", name: str = "") -> Fill | None:
        if qty <= 0:
            return None
        fill_price = self._fill_price(price, "BUY")
        cost = fill_price * qty
        fee = self.fee.buy_cost(fill_price, qty)
        if cost + fee > self._cash:
            qty = int((self._cash - fee) // fill_price)
            if qty <= 0:
                return None
            cost = fill_price * qty
            fee = self.fee.buy_cost(fill_price, qty)
        self._cash -= cost + fee
        self._seq += 1
        fill = Fill(code, "BUY", qty, fill_price, ts, fee, f"P{self._seq}")
        self._fills.append(fill)
        pos = self._positions.get(code)
        if pos is None:
            self._positions[code] = Position(code, qty, fill_price, ts, highest=fill_price, name=name, entry_reason=reason)
        else:
            total = pos.avg_price * pos.qty + fill_price * qty
            pos.qty += qty
            pos.avg_price = total / pos.qty
            pos.highest = max(pos.highest, fill_price)
        return fill

    def sell(self, code: str, qty: int, price: float, ts: datetime, reason: str = "") -> Fill | None:
        pos = self._positions.get(code)
        if pos is None or qty <= 0:
            return None
        qty = min(qty, pos.qty)
        fill_price = self._fill_price(price, "SELL")
        proceeds = fill_price * qty
        fee = self.fee.sell_cost(fill_price, qty)
        self._cash += proceeds - fee
        self._seq += 1
        fill = Fill(code, "SELL", qty, fill_price, ts, fee, f"P{self._seq}")
        self._fills.append(fill)
        buy_fee_share = self.fee.buy_cost(pos.avg_price, qty)
        pnl = (fill_price - pos.avg_price) * qty - fee - buy_fee_share
        pnl_pct = (fill_price / pos.avg_price - 1.0) * 100.0
        self._trades.append(
            Trade(code, qty, pos.avg_price, fill_price, pos.entry_ts, ts, pnl, pnl_pct, fee + buy_fee_share, reason, pos.name)
        )
        pos.qty -= qty
        if pos.qty <= 0:
            del self._positions[code]
        return fill
