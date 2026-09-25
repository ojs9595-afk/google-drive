"""실시간 매매 엔진: 피드 → 캔들 집계 → 전략/리스크 → 브로커, 대시보드 렌더링."""
from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime
from pathlib import Path

from rich.live import Live

from .broker.base import Broker
from .context import MarketContext
from .dashboard import Dashboard
from .data.base import DataFeed, Tick
from .data.candles import CandleStore
from .market import CLOSING_AUCTION_START, is_market_open, now_kst
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
        log_dir: str = "logs",
        universe_refresh_min: int = 0,
        max_universe: int = 25,
        screener_cfg: dict | None = None,
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
        self.log_dir = Path(log_dir)
        self.last_error: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._feed_task: asyncio.Task | None = None
        self._stop_requested = False
        self._current_day = None
        self._trades_written = 0
        self.universe_refresh_min = int(universe_refresh_min)
        self.max_universe = int(max_universe)
        self.screener_cfg = screener_cfg or {}
        self._last_universe_refresh: datetime | None = None
        self._universe_thread = None
        self._last_snapshot: dict | None = None
        self._offhours_logged = False
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
        """HTTP 스레드에서 호출됨. 루프가 상태를 바꾸는 순간과 겹치면 직전 스냅샷을 돌려준다."""
        try:
            snap = self._snapshot()
            self._last_snapshot = snap
            return snap
        except RuntimeError:  # dict/deque changed size during iteration
            if self._last_snapshot is not None:
                return self._last_snapshot
            raise

    def _snapshot(self) -> dict:
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
        min_bars = t.strategy.min_bars
        bars = {code: len(self.store.builder(code)) for code in self.store.codes()}
        feed_health = {
            "market_status": getattr(self.feed, "market_status", ""),
            "last_error": getattr(self.feed, "last_error", ""),
            "consecutive_failures": getattr(self.feed, "consecutive_failures", 0),
            "fetch_ok": getattr(self.feed, "fetch_ok", 0),
            "fetch_fail": getattr(self.feed, "fetch_fail", 0),
        }
        recs = self.recommendations()
        signal_log = [
            {"ts": e.ts.strftime("%H:%M:%S"), "code": e.code, "name": e.name, "kind": e.kind, "score": e.score, "price": e.price, "reasons": e.reasons, "executed": e.executed}
            for e in t.state.signal_log[-40:][::-1]
        ]
        return {
            "mode": self.mode, "feed": self.feed.name, "status": self.dashboard.status, "clock": clock, "auto_trade": t.auto_trade,
            "recommendations": recs, "signal_log": signal_log,
            "min_bars": min_bars, "bars": bars, "warming": [c for c, n in bars.items() if n < min_bars], "feed_health": feed_health,
            "last_tick": self._last_ts.strftime("%H:%M:%S") if self._last_ts else None, "last_error": self.last_error,
            "equity": equity, "cash": b.cash(), "realized": b.realized_pnl(), "unrealized": unreal, "day_pct": t.risk.daily_pnl_pct(),
            "trades": st.trades, "wins": st.wins, "losses": st.losses, "halted": st.halted_reason, "kelly_scale": t.risk.kelly_scale(),
            "watchlist": wl, "positions": positions, "events": events, "indices": indices, "market_bias": mkt_bias,
            "entry_block": sample.entry_block if sample else "", "news_count": len(ctx.news), "news": news, "sectors": sectors,
            "equity_history": self.equity_history[-600:],
        }

    def recommendations(self) -> dict:
        """마지막 시그널 기준 실시간 추천: 매수 / 매수대기(관심) / 매도·주의. 제안 수량·손절·목표가 포함."""
        t = self.trader
        p = getattr(t.strategy, "p", None)
        buy_t = getattr(p, "buy_threshold", 55.0)
        sell_t = getattr(p, "sell_threshold", -35.0)
        equity = t.equity()
        cash = self.broker.cash()
        buy, watch, sell = [], [], []
        for code, sig in t.state.last_signal.items():
            price = self.store.last_price.get(code, sig.price)
            tick = self.store.last_tick.get(code)
            pos = self.broker.position(code)
            blocked = next((r.replace("진입 보류: ", "") for r in sig.reasons if r.startswith("진입 보류")), "")
            stop, tp, dist = t.risk.initial_levels(price, sig.atr) if price > 0 else (0, 0, 0)
            qty = t.risk.position_size(equity, cash, price, sig.atr) if price > 0 else 0
            item = {
                "code": code, "name": t.name(code), "price": price, "change_pct": tick.change_pct if tick else 0.0,
                "score": sig.score, "strength": sig.strength, "action": sig.action.value, "regime": sig.regime,
                "reasons": [r for r in sig.reasons if not r.startswith("진입 보류")][:4], "blocked": blocked,
                "suggest": {"qty": qty, "stop": stop, "take_profit": tp, "risk_won": round(dist * qty), "reward_pct": round((tp / price - 1) * 100, 2) if price else 0, "risk_pct": round((1 - stop / price) * 100, 2) if price else 0},
                "has_position": pos is not None, "ts": sig.ts.strftime("%H:%M") if hasattr(sig.ts, "strftime") else "",
            }
            if pos is not None and sig.action.value == "SELL":
                item["label"] = "청산"
                sell.append(item)
            elif sig.score >= buy_t:
                item["label"] = "강력매수" if sig.score >= 80 else "매수"
                buy.append(item)
            elif sig.score <= sell_t:
                item["label"] = "강력매도" if sig.score <= -70 else "매도"
                sell.append(item)
            elif sig.score >= buy_t * 0.6:
                item["label"] = "관심"
                watch.append(item)
        buy.sort(key=lambda x: -x["score"])
        watch.sort(key=lambda x: -x["score"])
        sell.sort(key=lambda x: x["score"])
        return {"buy": buy[:10], "watch": watch[:10], "sell": sell[:10], "buy_threshold": buy_t, "sell_threshold": sell_t, "universe": len(t.state.last_signal)}

    # ----- 동적 유니버스 -----
    def add_codes(self, codes: dict[str, str]) -> list[str]:
        """새 종목을 관심종목에 추가 (과거 캔들 시드 + 피드 구독). 추가된 코드 목록 반환."""
        added = []
        for code, name in codes.items():
            if code in self.watchlist or len(self.watchlist) >= self.max_universe:
                continue
            try:
                candles = self.feed.minute_candles(code, self.warmup_candles, self.interval_min)
            except Exception as e:
                log.debug("%s 캔들 로드 실패: %s", code, e)
                candles = []
            self.store.seed(code, candles)
            self.watchlist[code] = name or code
            self.trader.names[code] = name or code
            self.context.names[code] = name or code
            added.append(code)
        if added:
            self.feed.subscribe(added)
            log.info("유니버스 추가: %s", ", ".join(f"{self.watchlist[c]}({c})" for c in added))
        return added

    def _maybe_refresh_universe(self, now: datetime) -> None:
        if self.universe_refresh_min <= 0 or self.feed.name == "sim":
            return
        if self._last_universe_refresh is not None and (now - self._last_universe_refresh).total_seconds() < self.universe_refresh_min * 60:
            return
        if self._universe_thread is not None and self._universe_thread.is_alive():
            return
        self._last_universe_refresh = now
        import threading

        def job():
            try:
                from .screener import screen

                sc = self.screener_cfg
                found = screen(sc.get("source", "naver"), int(sc.get("limit", 15)), float(sc.get("min_price", 2000)), float(sc.get("max_price", 500000)), exclude_codes=set(self.watchlist))
                if found:
                    self.add_codes(found)
            except Exception as e:
                log.warning("유니버스 갱신 실패: %s", e)

        self._universe_thread = threading.Thread(target=job, daemon=True, name="universe")
        self._universe_thread.start()

    # ----- 이벤트 -----
    async def on_tick(self, tick: Tick) -> None:
        try:
            self._handle_tick(tick)
        except Exception as e:  # 한 틱의 오류가 전체 루프를 죽이지 않도록 격리
            self.last_error = f"{type(e).__name__}: {e}"
            log.exception("틱 처리 오류 %s: %s", tick.code, e)

    def _handle_tick(self, tick: Tick) -> None:
        self._last_ts = tick.ts
        self.dashboard.sim_time = tick.ts if self.feed.name == "sim" else None
        # 일자 롤오버 (장기 실행 시): 강제청산 플래그 초기화
        day = tick.ts.date()
        if self._current_day is not None and day != self._current_day:
            self._closed_today = False
            self.equity_history.clear()
        self._current_day = day
        if tick.change_pct:
            self.context.update_stock_change(tick.code, tick.change_pct)
        # 정규장 밖의 실시간 시세는 표시용으로만 (가짜 캔들 방지)
        if self.feed.name != "sim" and not is_market_open(tick.ts):
            self.store.last_price[tick.code] = tick.price
            self.store.last_tick[tick.code] = tick
            if not self._offhours_logged:
                self._offhours_logged = True
                log.info("정규장(09:00~15:30) 밖입니다. 시세는 표시만 하고 캔들/시그널은 생성하지 않습니다.")
            self.dashboard.status = "장 마감 (시세 표시만)"
            return
        if self.feed.name != "sim" and self.dashboard.status.startswith("장 마감"):
            self.dashboard.status = "실시간 수신 중"
        closed = self.store.add_tick(tick)
        if closed is not None:
            if hasattr(self.feed, "index_tick"):
                it = self.feed.index_tick(closed.ts)  # 방금 닫힌 봉의 지수값 (미래 참조 방지)
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
        self._flush_trades()
        self._maybe_refresh_universe(tick.ts)

    def _flush_trades(self) -> None:
        """새로 완료된 거래를 logs/trades_YYYYMMDD.csv 에 추가 기록."""
        trades = self.broker.trades()
        if len(trades) <= self._trades_written:
            return
        try:
            from .performance import CSV_HEADER

            self.log_dir.mkdir(parents=True, exist_ok=True)
            # 시뮬레이션 거래는 별도 파일(sim_trades_*)에 기록해 실제 수익률과 섞이지 않게 한다
            prefix = "sim_trades" if self.feed.name == "sim" else "trades"
            path = self.log_dir / f"{prefix}_{(self._last_ts or now_kst()):%Y%m%d}.csv"
            new_file = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(CSV_HEADER)
                for t in trades[self._trades_written:]:
                    w.writerow([t.entry_ts.strftime("%Y-%m-%d %H:%M:%S"), t.exit_ts.strftime("%Y-%m-%d %H:%M:%S"), t.code, t.name or self.trader.name(t.code), t.qty, t.entry_price, t.exit_price, round(t.pnl), round(t.pnl_pct, 3), round(t.fees), t.reason, f"{self.feed.name}/{self.mode}"])
            self._trades_written = len(trades)
        except Exception as e:  # pragma: no cover
            log.debug("거래 CSV 기록 실패: %s", e)

    def run_on_loop(self, fn, timeout: float = 5.0):
        """다른 스레드에서 매매 상태를 바꾸는 호출을 이벤트 루프 스레드로 넘긴다 (경쟁 방지)."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return fn()
        import concurrent.futures

        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _call():
            if not fut.set_running_or_notify_cancel():
                return  # 호출자가 타임아웃으로 취소한 요청은 실행하지 않는다
            try:
                fut.set_result(fn())
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)

        loop.call_soon_threadsafe(_call)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise RuntimeError("엔진이 응답하지 않습니다. 잠시 후 다시 시도하세요.")

    def request_stop(self) -> None:
        """다른 스레드에서 호출 가능. 피드를 멈추고 이벤트 루프의 피드 태스크를 취소한다."""
        self._stop_requested = True
        self.feed.stop()
        for c in self.collectors:
            if hasattr(c, "stop"):
                c.stop()
        loop, task = self._loop, self._feed_task
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)

    @property
    def running(self) -> bool:
        return self._loop is not None and not self._stop_requested

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
        self._loop = asyncio.get_running_loop()
        # 웹 대시보드는 워밍업 전에 띄운다 (브라우저가 바로 열려도 페이지가 응답하도록)
        if self.web is not None:
            try:
                self.web.start()
                self.dashboard.status = f"초기화 중 · 웹 {self.web.url}"
            except OSError as e:
                log.warning("웹 대시보드 시작 실패: %s", e)
        try:
            self.warmup()
        except Exception as e:
            self.last_error = f"초기화 실패: {e}"
            log.exception("엔진 초기화 실패")
            self.dashboard.status = "초기화 실패"
            self._loop = None
            if self.web is not None:
                self.web.stop()
            raise
        if self._stop_requested:  # 워밍업 중 정지 요청
            self.dashboard.status = "종료"
            self._loop = None
            if self.web is not None:
                self.web.stop()
            return
        self.feed.on_tick(self.on_tick)
        self.dashboard.status = "실시간 수신 중"
        tasks = [asyncio.create_task(self.feed.run(), name="feed")]
        self._feed_task = tasks[0]
        if self._stop_requested:
            tasks[0].cancel()
        for c in self.collectors:
            tasks.append(asyncio.create_task(c.run(), name=type(c).__name__))
        if self.web is not None and self.web._server is not None:
            self.dashboard.status = f"실시간 수신 중 · 웹 {self.web.url}"
        if self.use_dashboard:
            self._live = Live(self.dashboard.render(), refresh_per_second=4, screen=False)
            self._live.start()
            tasks.append(asyncio.create_task(self._render_loop(), name="render"))
        try:
            await tasks[0]  # 피드 종료까지 대기
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.last_error = f"피드 오류: {e}"
            log.exception("피드 태스크 오류")
        finally:
            for step in (
                self._flush_trades,
                self.feed.stop,
                lambda: [c.stop() for c in self.collectors if hasattr(c, "stop")],
                lambda: [t.cancel() for t in tasks[1:]],
                lambda: self._live.update(self.dashboard.render()) if self._live is not None else None,
                lambda: self._live.stop() if self._live is not None else None,
                lambda: self.web.stop() if self.web is not None else None,
            ):
                try:
                    step()
                except Exception as e:  # pragma: no cover
                    log.debug("종료 단계 오류: %s", e)
            self.dashboard.status = "종료"
            self._loop = None
            self._feed_task = None

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
