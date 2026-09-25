"""설정(dict) → 피드/브로커/컨텍스트/엔진 조립. CLI(main.py)와 그래픽 앱(app.py)이 공유한다."""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime

from .config import build_pairs, build_quant_params, build_risk_params, build_rules, build_strategy_params
from .context import ContextParams, MarketContext, ScheduledEvent
from .notifier import Notifier
from .risk import RiskManager
from .strategy.ensemble import EnsembleStrategy

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """사용자가 고칠 수 있는 설정 문제 (API 키 누락 등)."""


def build_context(cfg: dict, watchlist: dict[str, str]) -> MarketContext:
    ctx_cfg = cfg.get("context") or {}
    params = ContextParams(**{k: v for k, v in ctx_cfg.items() if k in ContextParams.__dataclass_fields__})
    events = []
    for ev in ctx_cfg.get("events") or []:
        try:
            h, m = str(ev["time"]).split(":")
            d = datetime.strptime(str(ev["date"]), "%Y-%m-%d").date() if ev.get("date") else None
            events.append(ScheduledEvent(ev["name"], dtime(int(h), int(m)), int(ev.get("before_min", 10)), int(ev.get("after_min", 15)), d))
        except (KeyError, ValueError) as e:
            log.warning("이벤트 설정 무시 (%s): %s", ev, e)
    ctx = MarketContext(params, ctx_cfg.get("sectors") or {}, watchlist, events)
    ctx.enabled = bool(ctx_cfg.get("enabled", True))
    return ctx


def build_collectors(cfg: dict, ctx: MarketContext, watchlist: dict[str, str], kis_client=None) -> list:
    from .data.news import DEFAULT_RSS, IndexCollector, NewsCollector

    ctx_cfg = cfg.get("context") or {}
    if not ctx_cfg.get("enabled", True):
        return []
    rss = [(r["name"], r["url"]) for r in ctx_cfg.get("rss") or []] or DEFAULT_RSS
    stock_q = watchlist if ctx_cfg.get("stock_news", True) else {}
    return [
        NewsCollector(ctx, rss, stock_q, float(ctx_cfg.get("news_poll_sec", 60))),
        IndexCollector(ctx, float(ctx_cfg.get("index_poll_sec", 5)), kis_client),
    ]


def make_kis_client(cfg: dict):
    from .data.kis import KISClient

    k = cfg.get("kis") or {}
    if not k.get("app_key") or not k.get("app_secret"):
        raise ConfigError("한국투자증권 앱키/시크릿이 설정되지 않았습니다. 설정 화면(또는 config.yaml 의 kis 항목)에 입력하세요.")
    if not k.get("account"):
        raise ConfigError("한국투자증권 계좌번호(예: 12345678-01)가 설정되지 않았습니다.")
    return KISClient(k["app_key"], k["app_secret"], k["account"], paper=bool(k.get("paper", True)))


def make_feed_and_broker(cfg: dict, feed_name: str, mode: str):
    from .broker.paper import PaperBroker

    kis_client = None
    if feed_name == "kis" or mode == "live":
        kis_client = make_kis_client(cfg)

    if feed_name == "sim":
        from .data.sim import SimFeed

        s = cfg.get("sim") or {}
        feed = SimFeed(speed=float(s.get("speed", 60)), history_days=int(s.get("history_days", 3)), seed=s.get("seed"))
    elif feed_name == "naver":
        from .data.naver import NaverFeed

        feed = NaverFeed(poll_interval=float((cfg.get("naver") or {}).get("poll_interval", 1.5)))
    elif feed_name == "kis":
        from .data.kis import KISFeed

        feed = KISFeed(kis_client)
    else:
        raise ConfigError(f"알 수 없는 피드: {feed_name}")

    if mode == "live":
        from .broker.kis import KISBroker

        broker = KISBroker(kis_client)
    else:
        broker = PaperBroker(float(cfg.get("initial_cash", 10_000_000)))
    return feed, broker, kis_client


def resolve_watchlist(cfg: dict, codes: str | None = None, kis_client=None, use_screener: bool | None = None) -> dict[str, str]:
    base = dict(cfg.get("watchlist") or {})
    if codes:
        wl = {}
        for c in str(codes).split(","):
            c = c.strip()
            if not c:
                continue
            if not c.isdigit() or len(c) > 6:
                raise ConfigError(f"종목코드는 숫자 6자리여야 합니다: {c!r}")
            c = c.zfill(6)
            wl[c] = base.get(c, "")
        return wl
    sc = cfg.get("screener") or {}
    if use_screener if use_screener is not None else sc.get("enabled"):
        from .screener import screen

        try:
            found = screen(sc.get("source", "naver"), int(sc.get("limit", 15)), float(sc.get("min_price", 2000)), float(sc.get("max_price", 500000)), kis_client=kis_client)
        except Exception as e:
            log.warning("스크리너 실패: %s", e)
            found = {}
        if found:
            merged = dict(found)
            merged.update(base)
            return merged
    return base


def fill_names(feed, watchlist: dict[str, str]) -> None:
    """이름이 비어 있는 종목의 이름을 피드에서 조회 (네이버)."""
    if not hasattr(feed, "fetch_quote"):
        return
    for c, n in list(watchlist.items()):
        if n:
            continue
        try:
            t = feed.fetch_quote(c)
            if t and t.name:
                watchlist[c] = t.name
        except Exception:
            pass


def build_engine(
    cfg: dict,
    feed_name: str | None = None,
    mode: str | None = None,
    codes: str | None = None,
    auto_trade: bool | None = None,
    use_dashboard: bool = False,
    use_news: bool = True,
    web_host: str = "",
    web_port: int = 8787,
):
    """설정과 옵션으로 TradingEngine 을 조립해 돌려준다 (실행은 호출자가)."""
    from .engine import TradingEngine

    mode = mode or cfg.get("mode", "paper")
    feed_name = feed_name or cfg.get("feed", "sim")
    feed, broker, kis_client = make_feed_and_broker(cfg, feed_name, mode)
    watchlist = resolve_watchlist(cfg, codes, kis_client)
    if not watchlist:
        raise ConfigError("관심종목이 비어 있습니다. 설정에서 종목을 추가하세요.")
    if feed_name == "naver":
        fill_names(feed, watchlist)
    strategy = EnsembleStrategy(build_strategy_params(cfg))
    risk = RiskManager(build_risk_params(cfg))
    tg = cfg.get("telegram") or {}
    notifier = Notifier(tg.get("token", ""), tg.get("chat_id", ""), console=True)
    ctx = build_context(cfg, watchlist)
    collectors = build_collectors(cfg, ctx, watchlist, kis_client) if use_news else []
    engine = TradingEngine(
        feed, broker, strategy, risk, watchlist,
        interval_min=int(cfg.get("interval_min", 1)),
        auto_trade=(bool(cfg.get("auto_trade", True)) if auto_trade is None else auto_trade),
        notifier=notifier, context=ctx, collectors=collectors, mode=mode, use_dashboard=use_dashboard,
        quant_params=build_quant_params(cfg), pairs=build_pairs(cfg), rules=build_rules(cfg),
        web_host=web_host, web_port=web_port, log_dir=str(cfg.get("log_dir", "logs")),
        universe_refresh_min=int((cfg.get("screener") or {}).get("refresh_min", 0)) if (cfg.get("screener") or {}).get("enabled") else 0,
        max_universe=int((cfg.get("screener") or {}).get("max_universe", 25)),
        screener_cfg=cfg.get("screener") or {},
    )
    return engine
