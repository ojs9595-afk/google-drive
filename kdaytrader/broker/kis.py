"""KIS Open API 주문 브로커. 주문 전송 후 포지션/현금은 로컬에서 추적하고 잔고 조회로 동기화한다."""
from __future__ import annotations

import logging
from datetime import datetime

from ..data.kis import KISClient
from ..market import FeeModel
from .base import Broker, Fill, Position, Trade

log = logging.getLogger(__name__)


class KISBroker(Broker):
    name = "kis"

    def __init__(self, client: KISClient, fee: FeeModel | None = None):
        self.client = client
        self.fee = fee or FeeModel()
        self._cash = 0.0
        self._positions: dict[str, Position] = {}
        self._trades: list[Trade] = []
        self.sync()

    def sync(self) -> None:
        """계좌 잔고/보유종목 동기화."""
        try:
            d = self.client.balance()
        except Exception as e:
            log.error("잔고 조회 실패: %s", e)
            return
        out2 = d.get("output2") or [{}]
        summary = out2[0] if out2 else {}
        self._cash = float(summary.get("dnca_tot_amt") or summary.get("prvs_rcdl_excc_amt") or 0)
        now = datetime.now()
        for r in d.get("output1") or []:
            qty = int(float(r.get("hldg_qty", 0)))
            if qty <= 0:
                continue
            code = r["pdno"]
            if code in self._positions:
                self._positions[code].qty = qty
            else:
                self._positions[code] = Position(code, qty, float(r.get("pchs_avg_pric", 0)), now, name=r.get("prdt_name", ""))

    def cash(self) -> float:
        return self._cash

    def positions(self) -> dict[str, Position]:
        return self._positions

    def trades(self) -> list[Trade]:
        return self._trades

    def buy(self, code: str, qty: int, price: float, ts: datetime, reason: str = "", name: str = "") -> Fill | None:
        if qty <= 0:
            return None
        try:
            res = self.client.order(code, qty, "BUY")
        except Exception as e:
            log.error("매수 주문 실패 %s x%d: %s", code, qty, e)
            return None
        order_id = (res.get("output") or {}).get("ODNO", "")
        fee = self.fee.buy_cost(price, qty)
        self._cash -= price * qty + fee
        pos = self._positions.get(code)
        if pos is None:
            self._positions[code] = Position(code, qty, price, ts, highest=price, name=name, entry_reason=reason)
        else:
            total = pos.avg_price * pos.qty + price * qty
            pos.qty += qty
            pos.avg_price = total / pos.qty
        log.info("매수 주문 전송 %s x%d @%.0f (주문번호 %s)", code, qty, price, order_id)
        return Fill(code, "BUY", qty, price, ts, fee, order_id)

    def sell(self, code: str, qty: int, price: float, ts: datetime, reason: str = "") -> Fill | None:
        pos = self._positions.get(code)
        if pos is None or qty <= 0:
            return None
        qty = min(qty, pos.qty)
        try:
            res = self.client.order(code, qty, "SELL")
        except Exception as e:
            log.error("매도 주문 실패 %s x%d: %s", code, qty, e)
            return None
        order_id = (res.get("output") or {}).get("ODNO", "")
        fee = self.fee.sell_cost(price, qty)
        self._cash += price * qty - fee
        buy_fee = self.fee.buy_cost(pos.avg_price, qty)
        pnl = (price - pos.avg_price) * qty - fee - buy_fee
        self._trades.append(Trade(code, qty, pos.avg_price, price, pos.entry_ts, ts, pnl, (price / pos.avg_price - 1) * 100, fee + buy_fee, reason, pos.name))
        pos.qty -= qty
        if pos.qty <= 0:
            del self._positions[code]
        log.info("매도 주문 전송 %s x%d @%.0f (주문번호 %s) 사유=%s", code, qty, price, order_id, reason)
        return Fill(code, "SELL", qty, price, ts, fee, order_id)
