"""KIS Open API 주문 브로커.

- 봇이 매수한 포지션만 '관리 포지션'으로 다루고, 계좌의 기존 보유분(장기 투자 등)은 holdings 로만 보여 준다.
  따라서 강제청산/손절은 봇이 진입한 수량에만 적용된다.
- 관리 포지션은 JSON 파일에 영속화해 재시작 후에도 이어서 관리하며, 잔고 조회로 실제 보유 수량과 대조한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from ..data.kis import KISClient
from ..market import KST, FeeModel
from .base import Broker, Fill, Position, Trade

log = logging.getLogger(__name__)


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


class KISBroker(Broker):
    name = "kis"

    def __init__(self, client: KISClient, fee: FeeModel | None = None, state_path: str | Path | None = "logs/kis_positions.json"):
        self.client = client
        self.fee = fee or FeeModel()
        self._cash = 0.0
        self._positions: dict[str, Position] = {}
        self.holdings: dict[str, dict] = {}  # 계좌 전체 보유분 (표시/대조용)
        self._trades: list[Trade] = []
        self.state_path = Path(state_path) if state_path else None
        self.last_sync_error = ""
        self._load_state()
        self.sync()

    # ----- 영속화 -----
    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for d in data.get("positions", []):
                pos = Position(
                    d["code"], int(d["qty"]), float(d["avg_price"]), datetime.fromisoformat(d["entry_ts"]),
                    stop_price=float(d.get("stop_price", 0)), take_profit=float(d.get("take_profit", 0)), highest=float(d.get("highest", 0)),
                    atr_at_entry=float(d.get("atr_at_entry", 0)), risk_per_share=float(d.get("risk_per_share", 0)),
                    breakeven_moved=bool(d.get("breakeven_moved", False)), partial_done=bool(d.get("partial_done", False)),
                    name=d.get("name", ""), entry_reason=d.get("entry_reason", ""),
                )
                if pos.entry_ts.tzinfo is None:
                    pos.entry_ts = pos.entry_ts.replace(tzinfo=KST)
                self._positions[pos.code] = pos
            if self._positions:
                log.info("관리 포지션 %d개 복원: %s", len(self._positions), ", ".join(self._positions))
        except Exception as e:
            log.warning("관리 포지션 복원 실패: %s", e)

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"saved_at": datetime.now(tz=KST).isoformat(), "positions": [
                {"code": p.code, "qty": p.qty, "avg_price": p.avg_price, "entry_ts": p.entry_ts.isoformat(), "stop_price": p.stop_price, "take_profit": p.take_profit,
                 "highest": p.highest, "atr_at_entry": p.atr_at_entry, "risk_per_share": p.risk_per_share, "breakeven_moved": p.breakeven_moved,
                 "partial_done": p.partial_done, "name": p.name, "entry_reason": p.entry_reason}
                for p in self._positions.values()
            ]}
            self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log.debug("관리 포지션 저장 실패: %s", e)

    # ----- 잔고 동기화 -----
    def sync(self) -> None:
        """계좌 잔고/보유종목 동기화. 관리 포지션은 실제 보유 수량 이하로만 유지한다."""
        try:
            d = self.client.balance()
        except Exception as e:
            self.last_sync_error = str(e)
            log.error("잔고 조회 실패: %s", e)
            return
        self.last_sync_error = ""
        out2 = d.get("output2") or [{}]
        summary = out2[0] if out2 else {}
        # 주문가능(정산 반영) 금액을 우선 사용
        self._cash = _f(summary.get("prvs_rcdl_excc_amt")) or _f(summary.get("nxdy_excc_amt")) or _f(summary.get("dnca_tot_amt"))
        holdings: dict[str, dict] = {}
        for r in d.get("output1") or []:
            try:
                qty = int(_f(r.get("hldg_qty")))
                if qty <= 0:
                    continue
                code = str(r.get("pdno", "")).zfill(6)
                holdings[code] = {"qty": qty, "avg_price": _f(r.get("pchs_avg_pric")), "name": r.get("prdt_name", ""), "price": _f(r.get("prpr"))}
            except Exception as e:
                log.warning("잔고 행 파싱 실패 (%s): %s", r, e)
        self.holdings = holdings
        # 관리 포지션 대조: 계좌에 없거나 수량이 줄었으면 맞춘다
        for code in list(self._positions):
            held = holdings.get(code, {}).get("qty", 0)
            if held <= 0:
                log.warning("관리 포지션 %s 가 계좌에 없어 제거합니다 (외부 매도?)", code)
                del self._positions[code]
            elif held < self._positions[code].qty:
                log.warning("관리 포지션 %s 수량을 계좌 보유량 %d 로 조정", code, held)
                self._positions[code].qty = held
        self._save_state()

    def cash(self) -> float:
        return self._cash

    def positions(self) -> dict[str, Position]:
        return self._positions

    def trades(self) -> list[Trade]:
        return self._trades

    # ----- 주문 -----
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
        self._save_state()
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
        avg = pos.avg_price
        entry_ts = pos.entry_ts
        pname = pos.name
        pos.qty -= qty
        if pos.qty <= 0:
            del self._positions[code]
        self._cash += price * qty - fee
        buy_fee = self.fee.buy_cost(avg, qty)
        pnl = (price - avg) * qty - fee - buy_fee
        pnl_pct = (price / avg - 1) * 100 if avg > 0 else 0.0
        self._trades.append(Trade(code, qty, avg, price, entry_ts, ts, pnl, pnl_pct, fee + buy_fee, reason, pname))
        self._save_state()
        log.info("매도 주문 전송 %s x%d @%.0f (주문번호 %s) 사유=%s", code, qty, price, order_id, reason)
        return Fill(code, "SELL", qty, price, ts, fee, order_id)
