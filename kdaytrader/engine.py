"""실시간 매매 엔진: 피드 → 캔들 집계 → 전략/리스크 → 브로커, 대시보드 렌더링."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from rich.live import Live

from .broker.base import Broker
from .context import MarketContext
from .dashboard import Dashboard
from .data.base import DataFeed, Tick
from .data.candles import CandleStore
from .market import CLOSING_AUCTION_START, now_kst
from .notifier import Notifier
from .risk import RiskManager
from .strategy.base import Strategy
from .strategy.quant import QuantParams, QuantSignals
from .trader import Trader

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        feed: DataFeed,
        broker: Broker,
        strategy: Strategy,
        risk: RiskManager,
        watchlist: dict[str, str],
        interval_min: int = 1,
        auto_trade: bool = True,
        notifier: Notifier | None = None,
        context: MarketContext | None = None,
        collectors: list | None = None,
        mode: str = "paper",
        use_dashboard: bool = True,
        warmup_candles: int = 400,
        stop_at_close: bool = True,
        quant_params: QuantParams | None = None,
        pairs: dict[str, str] | None = None,
    ):
        self.feed = feed
        self.broker = broker
        self.store = CandleStore(interval_min * 60)
        self.context = context or MarketContext(names=watchlist)
        qp = quant_params or QuantParams()
        self.quant = QuantSignals(self.store, qp, pairs, self.context.stock_sectors) if qp.enabled else None
        self.trader = Trader(strategy, risk, broker, self.store, notifier, watchlist, auto_trade, context=self.context, quant=self.quant)
        self.watchlist = watchlist
        self.interval_min = interval_min
        self.collectors = collectors or []
        self.mode = mode
        self.use_dashboard = use_dashboard
        self.warmup_candles = warmup_candles
        self.stop_at_close = stop_at_close
        self.dashboard = Dashboard(self.trader, feed.name, mode, self.context)
        self._closed_today = False
        self._live: Live | None = None
        self._last_ts: datetime | None = None

    # ----- 초기화 -----
    def warmup(self) -> None:
        for code in self.watchlist:
            try:
                candles = self.feed.minute_candles(code, self.warmup_candles, self.interval_min)
            except Exception as e:
                log.warning("%s 과거 캔들 로드 실패: %s", code, e)
                candles = []
            self.store.seed(code, candles)
            log.info("%s(%s) 과거 캔들 %d개 로드", self.trader.name(code), code, len(candles))
        self.feed.subscribe(list(self.watchlist.keys()))

    # ----- 이벤트 -----
    async def on_tick(self, tick: Tick) -> None:
        self._last_ts = tick.ts
        self.dashboard.sim_time = tick.ts if self.feed.name == "sim" else None
        if tick.change_pct:
            self.context.update_stock_change(tick.code, tick.change_pct)
        closed = self.store.add_tick(tick)
        # 틱 단위 마이크로스트럭처 갱신 + 손절/익절/트레일링/긴급 청산
        self.trader.on_tick(tick)
        if closed is not None:
            sig = self.trader.on_candle(tick.code, tick.ts)
            if sig is not None and sig.action.value != "HOLD":
                log.info("%s %s 점수 %+.0f %s", self.trader.name(tick.code), sig.action.value, sig.score, ", ".join(sig.reasons[:3]))
        # 동시호가 진입 전 강제 청산
        if self.stop_at_close and tick.ts.time() >= self.trader.risk.p.force_close and not self._closed_today:
            self.trader.force_close_all(tick.ts)
            self._closed_today = True
            self.dashboard.status = "장 마감 청산 완료"

    async def _render_loop(self) -> None:
        while True:
            if self._live is not None:
                try:
                    self._live.update(self.dashboard.render())
                except Exception as e:  # pragma: no cover
                    log.debug("대시보드 렌더 오류: %s", e)
            await asyncio.sleep(0.5)

    # ----- 실행 -----
    async def run(self) -> None:
        self.warmup()
        self.feed.on_tick(self.on_tick)
        self.dashboard.status = "실시간 수신 중"
        tasks = [asyncio.create_task(self.feed.run(), name="feed")]
        for c in self.collectors:
            tasks.append(asyncio.create_task(c.run(), name=type(c).__name__))
        if self.use_dashboard:
            self._live = Live(self.dashboard.render(), refresh_per_second=4, screen=False)
            self._live.start()
            tasks.append(asyncio.create_task(self._render_loop(), name="render"))
        try:
            await tasks[0]  # 피드 종료까지 대기
        except asyncio.CancelledError:
            pass
        finally:
            self.feed.stop()
            for c in self.collectors:
                if hasattr(c, "stop"):
                    c.stop()
            for t in tasks[1:]:
                t.cancel()
            if self._live is not None:
                self._live.update(self.dashboard.render())
                self._live.stop()
            self.dashboard.status = "종료"

    def summary(self) -> dict:
        b = self.broker
        trades = b.trades()
        wins = [t for t in trades if t.pnl > 0]
        return {
            "최종자산": round(b.equity(self.store.last_price)),
            "실현손익": round(b.realized_pnl()),
            "거래횟수": len(trades),
            "승률(%)": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            "보유중": list(b.positions().keys()),
        }
