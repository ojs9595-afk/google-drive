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
from .strategy.rules import RuleSet
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
        rules: RuleSet | None = None,
        web_host: str = "",
        web_port: int = 8787,
    ):
        self.feed = feed
        self.broker = broker
        self.store = CandleStore(interval_min * 60)
        self.context = context or MarketContext(names=watchlist)
        qp = quant_params or QuantParams()
        self.quant = QuantSignals(self.store, qp, pairs, self.context.stock_sectors) if qp.enabled else None
        self.trader = Trader(strategy, risk, broker, self.store, notifier, watchlist, auto_trade, context=self.context, quant=self.quant, rules=rules)
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
        self.equity_history: list[tuple[str, float]] = []
        self.web = None
        if web_host:
            from .webui import WebServer

            self.web = WebServer(self, web_host, web_port)

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
        # 지수 과거 분봉 (베타·이벤트 스터디용) — 피드가 지원할 때만
        if hasattr(self.feed, "index_minute_candles"):
            try:
                self.context.seed_index_history("KOSPI", self.feed.index_minute_candles("KOSPI", self.warmup_candles))
            except Exception as e:
                log.debug("지수 분봉 로드 실패: %s", e)

    def last_ts(self) -> datetime:
        return self._last_ts or now_kst()

    # ----- 스냅샷 (웹 대시보드) -----
    def snapshot(self) -> dict:
        t = self.trader
        b = self.broker
        prices = self.store.last_price
        equity = b.equity(prices)
        unreal = sum(p.unrealized(prices.get(c, p.avg_price)) for c, p in b.positions().items())
        st = t.risk.stats
        ctx = self.context
        clock = (self._last_ts if self.feed.name == "sim" and self._last_ts else now_kst()).strftime("%Y-%m-%d %H:%M:%S")
        wl = []
        for code in self.store.codes():
            sig = t.state.last_signal.get(code)
            ind = t.state.last_indicators.get(code)
            tick = self.store.last_tick.get(code)

            def f(v):
                return None if v is None or v != v else float(v)

            wl.append(
                {
                    "code": code, "name": t.name(code), "price": prices.get(code, 0.0),
                    "change_pct": tick.change_pct if tick else 0.0,
                    "vwap": f(ind.vwap) if ind is not None else None, "rsi": f(ind.rsi) if ind is not None else None,
                    "adx": f(ind.adx) if ind is not None else None, "vol_ratio": f(ind.vol_ratio) if ind is not None else None,
                    "regime": {"trend": "추세", "range": "횡보"}.get(sig.regime if sig else "", "-"),
                    "beta": t.beta.get(code), "score": sig.score if sig else 0.0, "action": sig.action.value if sig else "HOLD",
                    "has_position": code in b.positions(), "reasons": (sig.reasons[:3] if sig else []),
                }
            )
        positions = [
            {
                "code": c, "name": t.name(c), "qty": p.qty, "avg_price": p.avg_price, "price": prices.get(c, p.avg_price),
                "pnl": p.unrealized(prices.get(c, p.avg_price)), "pnl_pct": p.unrealized_pct(prices.get(c, p.avg_price)),
                "r": p.r_multiple(prices.get(c, p.avg_price)), "stop": p.stop_price, "take_profit": p.take_profit, "entry": p.entry_ts.strftime("%H:%M"),
            }
            for c, p in b.positions().items()
        ]
        events = [
            {"ts": e.ts.strftime("%H:%M:%S"), "name": e.name, "side": e.side, "qty": e.qty, "price": e.price, "pnl": e.pnl, "reason": e.reason}
            for e in t.state.events[-30:][::-1]
        ]
        indices = [
            {"name": n, "value": v, "change_pct": chg, "momentum": ctx.index_momentum(n)} for n, (_, v, chg) in ctx.index.items()
        ]
        mkt_bias, _ = ctx.market_trend()
        sample = ctx.assess(next(iter(self.watchlist), ""), self.last_ts()) if self.watchlist else None
        news = [
            {"ts": n.ts.strftime("%H:%M"), "sentiment": n.sentiment, "tags": n.sectors[:2] + (["거시"] if n.macro else []) + [t.name(c) for c in n.codes[:2]], "title": n.title[:90]}
            for n in ctx.recent_news(15)
        ]
        sectors = [{"sector": s_, "rs": rs, "sentiment": sent, "count": cnt} for s_, rs, sent, cnt in ctx.sector_board()[:10]]
        return {
            "mode": self.mode, "feed": self.feed.name, "status": self.dashboard.status, "clock": clock, "auto_trade": t.auto_trade,
            "equity": equity, "cash": b.cash(), "realized": b.realized_pnl(), "unrealized": unreal, "day_pct": t.risk.daily_pnl_pct(),
            "trades": st.trades, "wins": st.wins, "losses": st.losses, "halted": st.halted_reason, "kelly_scale": t.risk.kelly_scale(),
            "watchlist": wl, "positions": positions, "events": events, "indices": indices, "market_bias": mkt_bias,
            "entry_block": sample.entry_block if sample else "", "news_count": len(ctx.news), "news": news, "sectors": sectors,
            "equity_history": self.equity_history[-600:],
        }

    # ----- 이벤트 -----
    async def on_tick(self, tick: Tick) -> None:
        self._last_ts = tick.ts
        self.dashboard.sim_time = tick.ts if self.feed.name == "sim" else None
        if tick.change_pct:
            self.context.update_stock_change(tick.code, tick.change_pct)
        closed = self.store.add_tick(tick)
        if closed is not None:
            if hasattr(self.feed, "index_tick"):
                it = self.feed.index_tick(tick.ts)
                if it:
                    self.context.update_index("KOSPI", it[0], it[1], tick.ts)
            self.equity_history.append((tick.ts.strftime("%H:%M"), self.broker.equity(self.store.last_price)))
            if len(self.equity_history) > 3000:
                self.equity_history = self.equity_history[-3000:]
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
        if self.web is not None:
            try:
                self.web.start()
                self.dashboard.status = f"실시간 수신 중 · 웹 {self.web.url}"
            except OSError as e:
                log.warning("웹 대시보드 시작 실패: %s", e)
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
            if self.web is not None:
                self.web.stop()

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
