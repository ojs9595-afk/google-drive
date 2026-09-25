"""매매 판단 코어: 캔들/가격 이벤트를 받아 전략·리스크·브로커를 연결한다.

실시간 엔진(engine.py)과 백테스터(backtest.py)가 동일한 로직을 공유하도록 분리했다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .broker.base import Broker, Fill
from .data.candles import CandleStore
from .indicators import compute_all
from .notifier import Notifier
from .risk import RiskManager
from .strategy.base import Action, Signal, Strategy
from .strategy.quant import QuantSignals
from .strategy.rules import RuleSet
from .context import MarketContext
from . import analytics

log = logging.getLogger(__name__)


@dataclass
class TradeEvent:
    ts: datetime
    code: str
    name: str
    side: str
    qty: int
    price: float
    reason: str
    pnl: float | None = None


@dataclass
class TraderState:
    last_signal: dict[str, Signal] = field(default_factory=dict)
    last_indicators: dict[str, pd.Series] = field(default_factory=dict)
    events: list[TradeEvent] = field(default_factory=list)
    candles_seen: int = 0


class Trader:
    def __init__(
        self,
        strategy: Strategy,
        risk: RiskManager,
        broker: Broker,
        store: CandleStore,
        notifier: Notifier | None = None,
        names: dict[str, str] | None = None,
        auto_trade: bool = True,
        window: int = 420,
        context: MarketContext | None = None,
        quant: QuantSignals | None = None,
        rules: RuleSet | None = None,
    ):
        self.strategy = strategy
        self.quant = quant
        self.rules = rules
        self.beta: dict[str, float] = {}  # 코스피 대비 롤링 베타 (gs-quant econometrics.beta)
        self.window = max(window, strategy.min_bars + 5)
        self.context = context or MarketContext()
        self.risk = risk
        self.broker = broker
        self.store = store
        self.notifier = notifier or Notifier(console=False)
        self.names = names or {}
        self.auto_trade = auto_trade
        self.state = TraderState()
        self._ind_params = getattr(strategy, "p", None).indicator_params() if hasattr(getattr(strategy, "p", None), "indicator_params") else None

    # ----- 유틸 -----
    def name(self, code: str) -> str:
        return self.names.get(code) or code

    def equity(self) -> float:
        return self.broker.equity(self.store.last_price)

    def _event(self, ts: datetime, code: str, side: str, fill: Fill | None, reason: str, pnl: float | None = None) -> None:
        if fill is None:
            return
        ev = TradeEvent(ts, code, self.name(code), side, fill.qty, fill.price, reason, pnl)
        self.state.events.append(ev)
        if len(self.state.events) > 1000:
            self.state.events = self.state.events[-1000:]
        pnl_txt = f" 손익 {pnl:+,.0f}원" if pnl is not None else ""
        self.notifier.send(f"[{side}] {ev.name}({code}) {fill.qty:,}주 @{fill.price:,.0f} | {reason}{pnl_txt}")

    # ----- 진입/청산 -----
    def _try_enter(self, sig: Signal, now: datetime) -> None:
        ok, why = self.risk.can_enter(now, sig.code, len(self.broker.positions()))
        if not ok:
            sig.reasons.append(f"진입 보류: {why}")
            return
        equity = self.equity()
        scale = self.risk.kelly_scale()
        if self.quant is not None and sig.code in self.quant.last:
            scale *= self.quant.last[sig.code].vol_scale
        qty = self.risk.position_size(equity, self.broker.cash(), sig.price, sig.atr, scale)
        if qty <= 0:
            sig.reasons.append("진입 보류: 수량 0 (자금/손절폭)")
            return
        fill = self.broker.buy(sig.code, qty, sig.price, now, reason="; ".join(sig.reasons[:3]), name=self.name(sig.code))
        if fill is None:
            return
        pos = self.broker.position(sig.code)
        if pos is not None:
            self.risk.attach(pos, sig.atr)
        self._event(now, sig.code, "BUY", fill, f"점수 {sig.score:+.0f} " + ", ".join(sig.reasons[:2]))

    def _exit(self, code: str, price: float, now: datetime, reason: str, qty: int | None = None) -> None:
        pos = self.broker.position(code)
        if pos is None:
            return
        q = qty or pos.qty
        fill = self.broker.sell(code, q, price, now, reason=reason)
        if fill is None:
            return
        trade = self.broker.trades()[-1]
        self.risk.record_trade(code, trade.pnl, now)
        self._event(now, code, "SELL", fill, reason, trade.pnl)

    # ----- 이벤트 처리 -----
    def on_tick(self, tick) -> None:
        """틱 수신: 마이크로스트럭처 상태 갱신 후 가격 점검."""
        if self.quant is not None:
            self.quant.on_tick(tick)
        self.on_price(tick.code, tick.price, tick.ts)

    def on_price(self, code: str, price: float, now: datetime, low: float | None = None, high: float | None = None) -> None:
        """틱(또는 캔들 고저)마다 보유 포지션의 손절/익절/트레일링을 점검."""
        pos = self.broker.position(code)
        if pos is None:
            return
        reason, exit_price = self.risk.check_exit(pos, price, now, low, high)
        if reason is None:
            urgent = self.context.urgent_exit(code, now)
            if urgent:
                reason, exit_price = urgent, price
        if reason:
            if self.auto_trade:
                self._exit(code, exit_price, now, reason)
            else:
                self.notifier.send(f"[청산 신호] {self.name(code)} {reason} @{exit_price:,.0f}")
            return
        pq = self.risk.partial_take_qty(pos, price)
        if pq > 0 and self.auto_trade:
            self._exit(code, price, now, "부분 익절", pq)
            pos = self.broker.position(code)
            if pos is None:
                return
        note = self.risk.update_trailing(pos, price)
        if note:
            log.debug("%s %s", code, note)

    def on_candle(self, code: str, now: datetime, ind: pd.DataFrame | None = None) -> Signal | None:
        """캔들이 닫힐 때 전략을 평가하고 진입/청산 결정을 내린다.

        ind: 미리 계산된 지표 DataFrame (백테스트 최적화용). None 이면 저장소에서 계산한다.
        """
        self.risk.ensure_day(now, self.equity())
        has_pos = self.broker.position(code) is not None
        if ind is None:
            df = self.store.df(code, n=self.window)
            if len(df) < self.strategy.min_bars:
                return None
            ind = compute_all(df, self._ind_params) if self._ind_params is not None else None
            if ind is None:
                sig = self.strategy.evaluate(code, df, has_pos)
                self.state.last_signal[code] = sig
                return sig
        elif len(ind) < self.strategy.min_bars:
            return None

        # 거시/섹터/뉴스 컨텍스트 → 점수 보정, 진입 차단, 청산 힌트
        ctx = self.context.assess(code, now)
        bias, entry_block, exit_hint, notes = ctx.bias, ctx.entry_block, ctx.exit_hint, list(ctx.notes)
        # 베타 조정: 시장 편향을 종목 베타로 스케일 (고베타 종목은 시장 방향에 더 민감)
        beta_adj = self._beta_adjustment(code, ind, ctx.market_bias)
        if beta_adj:
            bias += beta_adj
            notes.append(f"베타 {self.beta.get(code, 1.0):.2f} 조정 {beta_adj:+.0f}")
        # gs-quant 식 트리거/액션 규칙
        if self.rules is not None:
            pos = self.broker.position(code)
            price = float(ind["close"].iloc[-1])
            metrics = {}
            if pos is not None:
                metrics = {"r_multiple": pos.r_multiple(price), "unrealized_pct": pos.unrealized_pct(price), "holding_min": (now - pos.entry_ts).total_seconds() / 60.0}
            ro = self.rules.evaluate(ind, has_pos, now, metrics)
            bias += ro.bias
            entry_block = entry_block or ro.entry_block
            exit_hint = exit_hint or ro.exit_hint
            notes.extend(ro.notes)
        quant = self.quant.compute(code, ind, has_pos) if self.quant is not None else None
        sig = self.strategy.evaluate_with_indicators(code, ind, has_pos, bias=bias, entry_block=entry_block, exit_hint=exit_hint, quant=quant)
        if notes:
            sig.reasons.extend(notes)
        self.state.last_indicators[code] = ind.iloc[-1]
        self.state.last_signal[code] = sig
        self.state.candles_seen += 1

        if sig.action == Action.BUY:
            if self.auto_trade:
                self._try_enter(sig, now)
            else:
                self.notifier.send(f"[매수 신호] {self.name(code)} 점수 {sig.score:+.0f} ({sig.strength}) @{sig.price:,.0f} | " + ", ".join(sig.reasons[:3]))
        elif sig.action == Action.SELL and has_pos:
            exit_reasons = [r for r in sig.reasons if r.startswith("청산")]
            reason = "전략 청산: " + (", ".join(exit_reasons) if exit_reasons else f"점수 {sig.score:+.0f}")
            if self.auto_trade:
                self._exit(code, sig.price, now, reason)
            else:
                self.notifier.send(f"[매도 신호] {self.name(code)} {reason} @{sig.price:,.0f}")
        return sig

    def _beta_adjustment(self, code: str, ind: pd.DataFrame, market_bias: float) -> float:
        """코스피 분봉이 있으면 롤링 베타를 구해 시장 편향 보정치를 돌려준다."""
        idx = self.context.index_series("KOSPI")
        if idx is None or len(idx) < 40 or len(ind) < 40:
            return 0.0
        try:
            b = analytics.beta(ind["close"], idx, w=120)
            last = b.dropna()
            if last.empty:
                return 0.0
            beta_v = float(max(0.0, min(3.0, last.iloc[-1])))
        except Exception:
            return 0.0
        self.beta[code] = beta_v
        weight = self.context.p.market_trend_weight
        return market_bias * weight * (beta_v - 1.0)

    def force_close_all(self, now: datetime, reason: str = "장 마감 강제 청산") -> None:
        for code in list(self.broker.positions().keys()):
            price = self.store.last_price.get(code, self.broker.position(code).avg_price)
            self._exit(code, price, now, reason)
