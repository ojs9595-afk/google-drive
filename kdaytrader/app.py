"""그래픽 앱 백엔드: 엔진 수명주기, 설정 편집, 백테스트 작업, 차트 데이터, 뉴스, 로그를 JSON API 로 제공한다.

표준 라이브러리 HTTP 서버만 사용하며 127.0.0.1 에만 바인딩한다. 프런트엔드는 kdaytrader/web/ 의 정적 파일.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import traceback
from collections import deque
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from . import __version__
from .config import DEFAULTS, _merge, load_config
from .factory import ConfigError, build_engine, make_feed_and_broker, resolve_watchlist
from .indicators import compute_all
from .market import KST, is_market_open, now_kst, seconds_until_open
from .risk import RiskParams
from .strategy.ensemble import StrategyParams

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).with_name("web")

EDITABLE_SECTIONS = ["mode", "feed", "interval_min", "initial_cash", "auto_trade", "watchlist", "screener", "kis", "naver", "sim", "telegram", "strategy", "risk", "quant", "context", "rules", "web", "log_dir"]


def _clean(v):
    """JSON 직렬화 가능하도록 NaN/inf/datetime 정리."""
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalar
        try:
            return _clean(v.item())
        except Exception:
            return str(v)
    return v


class RingLogHandler(logging.Handler):
    def __init__(self, capacity: int = 500):
        super().__init__()
        self.records: deque[dict] = deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) if self.formatter else record.getMessage()
        except Exception:
            msg = str(record.msg)
        self._seq += 1
        self.records.append({"id": self._seq, "ts": time.strftime("%H:%M:%S", time.localtime(record.created)), "level": record.levelname, "name": record.name.replace("kdaytrader.", ""), "msg": msg})


class BacktestJob:
    def __init__(self):
        self.status = "idle"  # idle | running | done | error
        self.progress = ""
        self.result: dict | None = None
        self.error = ""
        self.started_at: float = 0.0
        self._thread: threading.Thread | None = None

    def start(self, fn) -> None:
        if self.status == "running":
            raise RuntimeError("백테스트가 이미 실행 중입니다.")
        self.status, self.error, self.result, self.progress = "running", "", None, "데이터 준비 중"
        self.started_at = time.time()

        def run():
            try:
                self.result = fn(self)
                self.status = "done"
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self.status = "error"
                log.exception("백테스트 실패")

        self._thread = threading.Thread(target=run, daemon=True, name="backtest")
        self._thread.start()

    def to_dict(self) -> dict:
        return {"status": self.status, "progress": self.progress, "error": self.error, "elapsed": round(time.time() - self.started_at, 1) if self.started_at else 0, "result": self.result}


class AppController:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path)
        self.cfg = load_config(str(self.config_path))
        self.engine = None
        self._thread: threading.Thread | None = None
        self.error = ""
        self.started_at: float = 0.0
        self.run_opts: dict = {}
        self.backtest = BacktestJob()
        self.loghandler = RingLogHandler()
        self.loghandler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.loghandler)
        if logging.getLogger().level > logging.INFO or logging.getLogger().level == logging.NOTSET:
            logging.getLogger().setLevel(logging.INFO)
        self._lock = threading.Lock()

    # ----- 상태 -----
    @property
    def engine_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        now = now_kst()
        open_ = is_market_open(now)
        eng = self.engine
        return {
            "version": __version__,
            "running": self.engine_running,
            "starting": bool(eng is not None and self.engine_running and eng.dashboard.status == "시작 중"),
            "engine_status": eng.dashboard.status if eng is not None else "대기",
            "last_error": self.error or (eng.last_error if eng is not None else ""),
            "run_opts": self.run_opts,
            "uptime_sec": round(time.time() - self.started_at) if self.engine_running and self.started_at else 0,
            "market_open": open_,
            "seconds_until_open": 0 if open_ else round(seconds_until_open(now)),
            "clock": now.strftime("%Y-%m-%d %H:%M:%S"),
            "config_path": str(self.config_path.resolve()),
            "config_exists": self.config_path.exists(),
            "kis_configured": bool((self.cfg.get("kis") or {}).get("app_key")) and bool((self.cfg.get("kis") or {}).get("app_secret")),
            "watchlist_count": len(self.cfg.get("watchlist") or {}),
            "backtest": self.backtest.status,
        }

    # ----- 엔진 -----
    def start(self, feed: str | None = None, mode: str | None = None, codes: str | None = None, signal_only: bool = False, use_news: bool = True, sim_speed: float | None = None) -> dict:
        with self._lock:
            if self.engine_running:
                raise RuntimeError("엔진이 이미 실행 중입니다. 먼저 정지하세요.")
            self.cfg = load_config(str(self.config_path))
            cfg = dict(self.cfg)
            if sim_speed is not None:
                cfg["sim"] = {**(cfg.get("sim") or {}), "speed": float(sim_speed)}
            self.error = ""
            engine = build_engine(cfg, feed, mode, codes=codes, auto_trade=(False if signal_only else None), use_dashboard=False, use_news=use_news)
            self.engine = engine
            self.run_opts = {"feed": engine.feed.name, "mode": engine.mode, "signal_only": signal_only, "codes": list(engine.watchlist.keys()), "news": use_news}
            self.started_at = time.time()

            def runner():
                try:
                    asyncio.run(engine.run())
                except Exception as e:
                    self.error = f"{type(e).__name__}: {e}"
                    log.exception("엔진 종료 (오류)")

            self._thread = threading.Thread(target=runner, daemon=True, name="engine")
            self._thread.start()
            return self.run_opts

    def stop(self, timeout: float = 10.0) -> bool:
        eng, th = self.engine, self._thread
        if eng is None or th is None:
            return True
        eng.request_stop()
        th.join(timeout)
        return not th.is_alive()

    def state(self) -> dict:
        eng = self.engine
        if eng is None:
            return {"running": False}
        try:
            snap = eng.snapshot()
        except Exception as e:
            return {"running": self.engine_running, "error": f"{type(e).__name__}: {e}"}
        snap["running"] = self.engine_running
        snap["last_error"] = eng.last_error or self.error
        return snap

    def toggle_auto(self) -> bool:
        if self.engine is None:
            raise RuntimeError("엔진이 실행 중이 아닙니다.")
        self.engine.trader.auto_trade = not self.engine.trader.auto_trade
        return self.engine.trader.auto_trade

    def close_all(self) -> int:
        if self.engine is None:
            raise RuntimeError("엔진이 실행 중이 아닙니다.")
        n = len(self.engine.broker.positions())
        self.engine.trader.force_close_all(self.engine.last_ts(), "수동 전량 청산")
        return n

    # ----- 차트 -----
    def chart(self, code: str, n: int = 240) -> dict:
        eng = self.engine
        if eng is None:
            raise RuntimeError("엔진을 먼저 시작하면 차트를 볼 수 있습니다.")
        code = code.zfill(6)
        if code not in eng.store.codes():
            raise RuntimeError(f"{code} 는 관심종목에 없습니다.")
        df = eng.store.df(code, include_current=True, n=max(n, 120))
        if df.empty:
            return {"code": code, "bars": []}
        try:
            ind = compute_all(df, eng.trader._ind_params)
        except Exception:
            ind = df
        ind = ind.iloc[-n:]
        cols = ["open", "high", "low", "close", "volume", "ema_fast", "ema_slow", "vwap", "bb_upper", "bb_lower", "rsi", "macd_hist", "st_line", "st_dir"]
        bars = {"t": [ts.strftime("%H:%M") for ts in ind.index], "date": [ts.strftime("%m-%d") for ts in ind.index]}
        for c in cols:
            bars[c] = _clean(ind[c].tolist()) if c in ind.columns else [None] * len(ind)
        first_ts = ind.index[0].to_pydatetime()
        markers = [
            {"t": e.ts.strftime("%H:%M"), "side": e.side, "price": e.price, "qty": e.qty, "reason": e.reason}
            for e in eng.trader.state.events if e.code == code and e.ts >= first_ts
        ]
        pos = eng.broker.position(code)
        position = {"avg_price": pos.avg_price, "stop": pos.stop_price, "take_profit": pos.take_profit, "qty": pos.qty} if pos else None
        sig = eng.trader.state.last_signal.get(code)
        return {"code": code, "name": eng.trader.name(code), "bars": bars, "markers": markers, "position": position, "signal": {"score": sig.score, "action": sig.action.value, "reasons": sig.reasons[:5], "regime": sig.regime} if sig else None}

    # ----- 설정 -----
    def _raw_config(self) -> dict:
        raw = {}
        if self.config_path.exists():
            try:
                raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                log.warning("config.yaml 파싱 실패: %s", e)
        return raw

    def config_get(self) -> dict:
        raw = self._raw_config()
        merged = _merge({k: v for k, v in DEFAULTS.items() if k in EDITABLE_SECTIONS}, raw)
        # 환경변수 플레이스홀더는 비워서 보여준다
        for k in ("app_key", "app_secret", "account"):
            v = (merged.get("kis") or {}).get(k, "")
            if isinstance(v, str) and v.startswith("${"):
                merged["kis"][k] = ""
        for k in ("token", "chat_id"):
            v = (merged.get("telegram") or {}).get(k, "")
            if isinstance(v, str) and v.startswith("${"):
                merged["telegram"][k] = ""
        wl = merged.get("watchlist") or {}
        if isinstance(wl, list):
            wl = {str(c): "" for c in wl}
        merged["watchlist"] = {str(k).zfill(6): (v or "") for k, v in wl.items()}
        merged["_schema"] = {
            "strategy": {f.name: f.default for f in fields(StrategyParams) if not isinstance(f.default, tuple)},
            "risk": {f.name: (f.default.strftime("%H:%M") if hasattr(f.default, "strftime") else f.default) for f in fields(RiskParams)},
        }
        return merged

    def config_save(self, data: dict) -> dict:
        raw = self._raw_config()
        for k in EDITABLE_SECTIONS:
            if k in data:
                raw[k] = data[k]
        wl = raw.get("watchlist") or {}
        if isinstance(wl, list):
            wl = {str(c): "" for c in wl}
        raw["watchlist"] = {str(k).zfill(6): (v or "") for k, v in wl.items() if str(k).strip()}
        # 숫자형 정리
        for sec in ("strategy", "risk", "quant", "context"):
            if isinstance(raw.get(sec), dict):
                raw[sec] = {k: v for k, v in raw[sec].items() if v not in ("", None)}
        try:
            load_config_from_dict(raw)  # 검증
        except Exception as e:
            raise RuntimeError(f"설정 값 오류: {e}")
        self.config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.cfg = load_config(str(self.config_path))
        return self.config_get()

    # ----- 백테스트 -----
    def backtest_start(self, days: int = 10, codes: str | None = None, feed: str = "sim", seed: int = 1) -> None:
        cfg = load_config(str(self.config_path))

        def job(j: BacktestJob) -> dict:
            from .backtest import Backtester
            from .config import build_pairs, build_quant_params, build_risk_params, build_rules, build_strategy_params
            from .strategy.ensemble import EnsembleStrategy

            watchlist = resolve_watchlist(cfg, codes)
            if not watchlist:
                raise RuntimeError("관심종목이 비어 있습니다.")
            data = {}
            if feed == "sim":
                from .data.sim import DEFAULT_BASE_PRICES, generate_history

                for i, code in enumerate(watchlist):
                    j.progress = f"시뮬레이션 데이터 생성 {i + 1}/{len(watchlist)}"
                    data[code] = generate_history(code, int(days), DEFAULT_BASE_PRICES.get(code, 50_000), seed=int(seed) + i * 100)
            else:
                f, _, _ = make_feed_and_broker(cfg, feed, "paper")
                for i, code in enumerate(watchlist):
                    j.progress = f"{watchlist.get(code) or code} 분봉 로드 {i + 1}/{len(watchlist)}"
                    try:
                        data[code] = f.minute_candles(code, int(days) * 390, int(cfg.get("interval_min", 1)))
                    except Exception as e:
                        log.warning("%s 로드 실패: %s", code, e)
                data = {k: v for k, v in data.items() if len(v) >= 100}
                if not data:
                    raise RuntimeError("분봉 데이터를 받지 못했습니다 (네트워크/피드 확인).")
            j.progress = "전략 실행 중"
            bt = Backtester(EnsembleStrategy(build_strategy_params(cfg)), build_risk_params(cfg), float(cfg.get("initial_cash", 10_000_000)), interval_min=int(cfg.get("interval_min", 1)), quant_params=build_quant_params(cfg), pairs=build_pairs(cfg), rules=build_rules(cfg))
            res = bt.run(data, watchlist)
            curve = res.equity_curve
            step = max(1, len(curve) // 800)
            pts = [(ts.strftime("%m-%d %H:%M"), float(v)) for ts, v in curve.iloc[::step].items()]
            peak = curve.cummax()
            dd = ((curve / peak - 1.0) * 100.0).iloc[::step]
            trades = [
                {"code": t.code, "name": watchlist.get(t.code) or t.name or t.code, "qty": t.qty, "entry": t.entry_ts.strftime("%m-%d %H:%M"), "exit": t.exit_ts.strftime("%H:%M"), "entry_price": t.entry_price, "exit_price": t.exit_price, "pnl": round(t.pnl), "pnl_pct": round(t.pnl_pct, 2), "reason": t.reason, "minutes": round(t.holding_minutes)}
                for t in res.trades
            ]
            per_code = {}
            for t in res.trades:
                d = per_code.setdefault(t.code, {"name": watchlist.get(t.code) or t.code, "trades": 0, "pnl": 0.0, "wins": 0})
                d["trades"] += 1
                d["pnl"] += t.pnl
                d["wins"] += 1 if t.pnl > 0 else 0
            return {"summary": _clean(res.summary()), "equity": pts, "drawdown": [float(x) for x in dd.tolist()], "trades": trades, "per_code": _clean(per_code), "params": {"days": days, "feed": feed, "codes": list(watchlist.keys())}}

        self.backtest.start(job)

    # ----- 뉴스 / 거시 -----
    def news(self, fetch: bool = False, stocks: bool = True) -> dict:
        from .data.news import IndexCollector, NewsCollector
        from .factory import build_context

        cfg = self.cfg
        if self.engine is not None:
            ctx = self.engine.context
            watchlist = self.engine.watchlist
        else:
            ctx = getattr(self, "_news_ctx", None)
            if ctx is None:
                ctx = self._news_ctx = build_context(cfg, cfg.get("watchlist") or {})
            watchlist = cfg.get("watchlist") or {}
        errors = []
        if fetch:
            try:
                IndexCollector(ctx).fetch_once()
            except Exception as e:
                errors.append(f"지수: {e}")
            try:
                nc = NewsCollector(ctx, stock_queries=watchlist if stocks else {})
                nc.fetch_once()
                if nc.last_error:
                    errors.append(nc.last_error)
            except Exception as e:
                errors.append(f"뉴스: {e}")
        now = now_kst()
        mkt_bias, mkt_txt = ctx.market_trend()
        return _clean({
            "indices": [{"name": n, "value": v, "change_pct": chg, "momentum": ctx.index_momentum(n), "ts": ts.strftime("%H:%M:%S")} for n, (ts, v, chg) in ctx.index.items()],
            "market_bias": mkt_bias, "market_text": mkt_txt,
            "sectors": [{"sector": s, "rs": rs, "sentiment": sent, "count": cnt} for s, rs, sent, cnt in ctx.sector_board()],
            "news": [{"ts": n.ts.strftime("%m-%d %H:%M"), "title": n.title, "source": n.source, "link": n.link, "sentiment": n.sentiment, "sectors": n.sectors, "codes": [watchlist.get(c) or c for c in n.codes], "macro": n.macro} for n in ctx.recent_news(60)],
            "assessments": [{"code": c, "name": nm or c, **{k: getattr(a, k) for k in ("bias", "entry_block", "exit_hint", "notes", "market_bias", "sector_bias", "news_bias")}} for c, nm in watchlist.items() for a in [ctx.assess(c, now)]],
            "errors": errors, "news_count": len(ctx.news), "updated": ctx.updated_at.strftime("%H:%M:%S") if ctx.updated_at else None,
        })

    def screen(self, source: str = "naver", limit: int = 15) -> dict:
        from .screener import screen

        kis_client = None
        if source == "kis":
            from .factory import make_kis_client

            kis_client = make_kis_client(self.cfg)
        return screen(source, limit, kis_client=kis_client)

    def lookup(self, code: str) -> dict:
        from .data.naver import NaverFeed

        t = NaverFeed().fetch_quote(code.zfill(6))
        if t is None:
            raise RuntimeError("종목을 찾지 못했습니다.")
        return {"code": code.zfill(6), "name": t.name, "price": t.price, "change_pct": t.change_pct}

    def performance(self, days: int | None = 90) -> dict:
        from .performance import load_trade_files, merge_trades, performance_report, trades_from_broker

        cfg = self.cfg
        files = load_trade_files(cfg.get("log_dir", "logs"))
        session = trades_from_broker(self.engine.broker.trades(), self.engine.watchlist) if self.engine is not None else []
        trades = merge_trades(files, session)
        rep = performance_report(trades, days or None, float(cfg.get("initial_cash", 10_000_000)))
        rep["open_positions"] = []
        if self.engine is not None:
            prices = self.engine.store.last_price
            rep["open_positions"] = [{"code": c, "name": self.engine.trader.name(c), "qty": p.qty, "avg_price": p.avg_price, "price": prices.get(c, p.avg_price), "pnl": p.unrealized(prices.get(c, p.avg_price))} for c, p in self.engine.broker.positions().items()]
        rep["sources"] = sorted({t["source"] for t in trades})
        return rep

    def performance_csv(self, days: int | None = None) -> str:
        from .performance import load_trade_files, merge_trades, trades_from_broker, trades_to_csv

        files = load_trade_files(self.cfg.get("log_dir", "logs"))
        session = trades_from_broker(self.engine.broker.trades(), self.engine.watchlist) if self.engine is not None else []
        trades = merge_trades(files, session)
        if days:
            from datetime import timedelta

            cutoff = now_kst() - timedelta(days=days)
            trades = [t for t in trades if t["exit_ts"] >= cutoff]
        return trades_to_csv(trades)

    def diagnose(self) -> dict:
        """인터넷 데이터 소스 연결 진단 (각 항목 개별 타임아웃)."""
        from .diagnostics import run_diagnostics

        return run_diagnostics(self.cfg)

    def logs(self, since: int = 0) -> dict:
        recs = [r for r in self.loghandler.records if r["id"] > since]
        return {"records": recs[-300:], "last_id": self.loghandler._seq}


def load_config_from_dict(raw: dict) -> dict:
    """저장 전 검증용: 파라미터 dataclass 생성이 되는지 확인."""
    from .config import build_pairs, build_quant_params, build_risk_params, build_rules, build_strategy_params
    from .config import _expand

    cfg = _expand(_merge(DEFAULTS, raw))
    build_strategy_params(cfg)
    build_risk_params(cfg)
    build_quant_params(cfg)
    build_pairs(cfg)
    build_rules(cfg)
    float(cfg.get("initial_cash", 0))
    int(cfg.get("interval_min", 1))
    return cfg


# ---------------------------------------------------------------------------
# HTTP 서버
# ---------------------------------------------------------------------------
class AppServer:
    def __init__(self, controller: AppController, host: str = "127.0.0.1", port: int = 8787):
        self.ctl = controller
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _make_handler(self):
        ctl = self.ctl
        static = {p.name: p for p in WEB_DIR.iterdir()} if WEB_DIR.exists() else {}
        ctypes = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}

        class Handler(BaseHTTPRequestHandler):
            server_version = "KDayTrader/" + __version__

            def log_message(self, *a):
                pass

            def _json(self, obj, code: int = 200) -> None:
                body = json.dumps(_clean(obj), ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _file(self, name: str) -> None:
                p = static.get(name)
                if p is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctypes.get(p.suffix, "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return {}

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    if u.path == "/" or u.path == "/index.html":
                        return self._file("index.html")
                    if u.path.startswith("/static/"):
                        return self._file(u.path.split("/static/", 1)[1])
                    if u.path == "/api/status":
                        return self._json(ctl.status())
                    if u.path == "/api/state":
                        return self._json(ctl.state())
                    if u.path == "/api/chart":
                        return self._json(ctl.chart(q.get("code", ""), int(q.get("n", 240))))
                    if u.path == "/api/config":
                        return self._json(ctl.config_get())
                    if u.path == "/api/backtest":
                        return self._json(ctl.backtest.to_dict())
                    if u.path == "/api/news":
                        return self._json(ctl.news(fetch=q.get("fetch") == "1", stocks=q.get("stocks", "1") == "1"))
                    if u.path == "/api/logs":
                        return self._json(ctl.logs(int(q.get("since", 0))))
                    if u.path == "/api/lookup":
                        return self._json(ctl.lookup(q.get("code", "")))
                    if u.path == "/api/screen":
                        return self._json({"result": ctl.screen(q.get("source", "naver"), int(q.get("limit", 15)))})
                    if u.path == "/api/diagnose":
                        return self._json(ctl.diagnose())
                    if u.path == "/api/performance":
                        return self._json(ctl.performance(int(q.get("days", 90)) or None))
                    if u.path == "/api/performance.csv":
                        body = ctl.performance_csv(int(q.get("days", 0)) or None).encode("utf-8-sig")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/csv; charset=utf-8")
                        self.send_header("Content-Disposition", "attachment; filename=trades.csv")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self._json({"error": "not found"}, 404)
                except (RuntimeError, ConfigError, ValueError) as e:
                    self._json({"error": str(e)}, 400)
                except Exception as e:  # pragma: no cover
                    log.exception("API 오류 %s", self.path)
                    self._json({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]}, 500)

            def do_POST(self):
                u = urlparse(self.path)
                body = self._body()
                try:
                    if u.path == "/api/start":
                        return self._json({"ok": True, "run": ctl.start(body.get("feed"), body.get("mode"), body.get("codes"), bool(body.get("signal_only")), bool(body.get("news", True)), body.get("sim_speed"))})
                    if u.path == "/api/stop":
                        return self._json({"ok": ctl.stop()})
                    if u.path == "/api/toggle_auto":
                        return self._json({"auto_trade": ctl.toggle_auto()})
                    if u.path == "/api/close_all":
                        return self._json({"closed": ctl.close_all()})
                    if u.path == "/api/config":
                        return self._json(ctl.config_save(body))
                    if u.path == "/api/backtest":
                        ctl.backtest_start(int(body.get("days", 10)), body.get("codes"), body.get("feed", "sim"), int(body.get("seed", 1)))
                        return self._json({"ok": True})
                    self._json({"error": "not found"}, 404)
                except (RuntimeError, ConfigError, ValueError) as e:
                    self._json({"error": str(e)}, 400)
                except Exception as e:  # pragma: no cover
                    log.exception("API 오류 %s", self.path)
                    self._json({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]}, 500)

        return Handler

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="appserver")
        self._thread.start()
        log.info("앱 서버 시작: %s", self.url)

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        try:
            self.ctl.stop(5)
        except Exception:
            pass
        if self._server is not None:
            self._server.shutdown()
            self._server = None


def find_free_port(host: str, preferred: int) -> int:
    import socket

    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return preferred
