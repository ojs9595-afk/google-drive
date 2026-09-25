#!/usr/bin/env python3
"""한국주식 실시간 기술적 분석 데이트레이딩 프로그램 CLI.

  python main.py run       --feed sim|naver|kis --mode paper|live
  python main.py backtest  --days 10 --codes 005930,000660
  python main.py screen
  python main.py news
  python main.py demo      (시뮬레이션 피드로 즉시 체험)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, time as dtime
from pathlib import Path

from kdaytrader.config import build_pairs, build_quant_params, build_risk_params, build_strategy_params, load_config
from kdaytrader.context import ContextParams, MarketContext, ScheduledEvent
from kdaytrader.market import KST, now_kst
from kdaytrader.notifier import Notifier
from kdaytrader.risk import RiskManager
from kdaytrader.strategy.ensemble import EnsembleStrategy


def setup_logging(log_dir: str, quiet_console: bool) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(Path(log_dir) / f"kdaytrader_{now_kst():%Y%m%d}.log", encoding="utf-8")]
    if not quiet_console:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def build_context(cfg: dict, watchlist: dict[str, str]) -> MarketContext:
    ctx_cfg = cfg.get("context") or {}
    params = ContextParams(**{k: v for k, v in ctx_cfg.items() if k in ContextParams.__dataclass_fields__})
    events = []
    for ev in ctx_cfg.get("events") or []:
        h, m = str(ev["time"]).split(":")
        d = None
        if ev.get("date"):
            d = datetime.strptime(str(ev["date"]), "%Y-%m-%d").date()
        events.append(ScheduledEvent(ev["name"], dtime(int(h), int(m)), int(ev.get("before_min", 10)), int(ev.get("after_min", 15)), d))
    ctx = MarketContext(params, ctx_cfg.get("sectors") or {}, watchlist, events)
    ctx.enabled = bool(ctx_cfg.get("enabled", True))
    return ctx


def build_collectors(cfg: dict, ctx: MarketContext, watchlist: dict[str, str], kis_client=None) -> list:
    from kdaytrader.data.news import DEFAULT_RSS, IndexCollector, NewsCollector

    ctx_cfg = cfg.get("context") or {}
    if not ctx_cfg.get("enabled", True):
        return []
    rss = [(r["name"], r["url"]) for r in ctx_cfg.get("rss") or []] or DEFAULT_RSS
    stock_q = watchlist if ctx_cfg.get("stock_news", True) else {}
    return [
        NewsCollector(ctx, rss, stock_q, float(ctx_cfg.get("news_poll_sec", 60))),
        IndexCollector(ctx, float(ctx_cfg.get("index_poll_sec", 5)), kis_client),
    ]


def make_feed_and_broker(cfg: dict, feed_name: str, mode: str, watchlist: dict[str, str]):
    from kdaytrader.broker.paper import PaperBroker

    kis_client = None
    if feed_name == "kis" or mode == "live":
        from kdaytrader.data.kis import KISClient

        k = cfg["kis"]
        kis_client = KISClient(k["app_key"], k["app_secret"], k["account"], paper=bool(k.get("paper", True)))

    if feed_name == "sim":
        from kdaytrader.data.sim import SimFeed

        s = cfg["sim"]
        feed = SimFeed(speed=float(s.get("speed", 60)), history_days=int(s.get("history_days", 3)), seed=s.get("seed"))
    elif feed_name == "naver":
        from kdaytrader.data.naver import NaverFeed

        feed = NaverFeed(poll_interval=float(cfg["naver"].get("poll_interval", 1.5)))
    elif feed_name == "kis":
        from kdaytrader.data.kis import KISFeed

        feed = KISFeed(kis_client)
    else:
        raise SystemExit(f"알 수 없는 feed: {feed_name}")

    if mode == "live":
        from kdaytrader.broker.kis import KISBroker

        broker = KISBroker(kis_client)
    else:
        broker = PaperBroker(float(cfg["initial_cash"]))
    return feed, broker, kis_client


def resolve_watchlist(cfg: dict, args, kis_client=None) -> dict[str, str]:
    if getattr(args, "codes", None):
        wl = {}
        for c in args.codes.split(","):
            c = c.strip().zfill(6)
            wl[c] = cfg["watchlist"].get(c, "")
        return wl
    sc = cfg.get("screener") or {}
    if sc.get("enabled"):
        from kdaytrader.screener import screen

        found = screen(sc.get("source", "naver"), int(sc.get("limit", 15)), float(sc.get("min_price", 2000)), float(sc.get("max_price", 500000)), kis_client=kis_client)
        if found:
            merged = dict(found)
            merged.update(cfg["watchlist"])
            return merged
    return dict(cfg["watchlist"])


def cmd_run(args, cfg: dict) -> None:
    from kdaytrader.engine import TradingEngine

    mode = args.mode or cfg["mode"]
    feed_name = args.feed or cfg["feed"]
    if mode == "live":
        print("⚠ 실계좌 주문 모드입니다. 손실이 발생할 수 있습니다. 계속하려면 'yes' 입력:", end=" ")
        if input().strip().lower() != "yes":
            raise SystemExit("취소")
    setup_logging(cfg["log_dir"], quiet_console=not args.no_dashboard)
    feed, broker, kis_client = make_feed_and_broker(cfg, feed_name, mode, cfg["watchlist"])
    watchlist = resolve_watchlist(cfg, args, kis_client)
    if feed_name == "naver" and not all(watchlist.values()):
        try:
            for c, n in watchlist.items():
                if not n:
                    t = feed.fetch_quote(c)
                    if t and t.name:
                        watchlist[c] = t.name
        except Exception:
            pass
    strategy = EnsembleStrategy(build_strategy_params(cfg))
    risk = RiskManager(build_risk_params(cfg))
    tg = cfg.get("telegram") or {}
    notifier = Notifier(tg.get("token", ""), tg.get("chat_id", ""), console=True)
    ctx = build_context(cfg, watchlist)
    collectors = build_collectors(cfg, ctx, watchlist, kis_client) if not args.no_news else []
    engine = TradingEngine(
        feed, broker, strategy, risk, watchlist,
        interval_min=int(cfg["interval_min"]), auto_trade=bool(cfg["auto_trade"]) and not args.signal_only,
        notifier=notifier, context=ctx, collectors=collectors, mode=mode, use_dashboard=not args.no_dashboard,
        quant_params=build_quant_params(cfg), pairs=build_pairs(cfg),
    )
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        pass
    print(json.dumps(engine.summary(), ensure_ascii=False, indent=2))


def cmd_demo(args, cfg: dict) -> None:
    cfg = dict(cfg)
    cfg["feed"] = "sim"
    cfg["mode"] = "paper"
    cfg["sim"] = {**cfg.get("sim", {}), "speed": args.speed}
    args.mode, args.feed = "paper", "sim"
    if args.no_news is None:
        args.no_news = True
    cmd_run(args, cfg)


def cmd_backtest(args, cfg: dict) -> None:
    from kdaytrader.backtest import Backtester, grid_search

    setup_logging(cfg["log_dir"], quiet_console=False)
    watchlist = resolve_watchlist(cfg, args)
    feed_name = args.feed or ("sim" if cfg["feed"] == "sim" else cfg["feed"])
    data = {}
    if feed_name == "sim":
        from kdaytrader.data.sim import DEFAULT_BASE_PRICES, generate_history

        for i, code in enumerate(watchlist):
            data[code] = generate_history(code, args.days, DEFAULT_BASE_PRICES.get(code, 50_000), seed=(args.seed or 0) + i * 100)
    else:
        feed, _, _ = make_feed_and_broker(cfg, feed_name, "paper", watchlist)
        for code in watchlist:
            try:
                data[code] = feed.minute_candles(code, args.days * 390, int(cfg["interval_min"]))
                print(f"{watchlist.get(code) or code}: 캔들 {len(data[code])}개")
            except Exception as e:
                print(f"{code} 데이터 로드 실패: {e}")
    if args.csv_dir:
        import pandas as pd
        from kdaytrader.data.base import Candle

        for f in Path(args.csv_dir).glob("*.csv"):
            df = pd.read_csv(f, parse_dates=["ts"])
            data[f.stem] = [Candle(r.ts.to_pydatetime().replace(tzinfo=KST) if r.ts.tzinfo is None else r.ts.to_pydatetime(), r.open, r.high, r.low, r.close, int(r.volume)) for r in df.itertuples()]
    if not data:
        raise SystemExit("백테스트 데이터가 없습니다.")
    sp, rp = build_strategy_params(cfg), build_risk_params(cfg)
    if args.grid:
        grid = json.loads(args.grid)
        results = grid_search(data, grid, sp, rp, initial_cash=float(cfg["initial_cash"]), interval_min=int(cfg["interval_min"]), quant_params=build_quant_params(cfg), pairs=build_pairs(cfg))
        for ov, summ in results[:15]:
            print(ov, summ)
        return
    bt = Backtester(EnsembleStrategy(sp), rp, float(cfg["initial_cash"]), interval_min=int(cfg["interval_min"]), quant_params=build_quant_params(cfg), pairs=build_pairs(cfg))
    res = bt.run(data, watchlist)
    print(json.dumps(res.summary(), ensure_ascii=False, indent=2))
    if args.trades:
        for t in res.trades:
            print(f"{t.entry_ts:%m-%d %H:%M} → {t.exit_ts:%H:%M} {watchlist.get(t.code) or t.code:10s} {t.qty:5d}주 {t.entry_price:>9,.0f} → {t.exit_price:>9,.0f} {t.pnl:+10,.0f} ({t.pnl_pct:+.2f}%) {t.reason}")
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        res.equity_curve.to_csv(Path(args.out) / "equity_curve.csv", header=["equity"])
        import pandas as pd

        pd.DataFrame([t.__dict__ for t in res.trades]).to_csv(Path(args.out) / "trades.csv", index=False)
        print(f"결과 저장: {args.out}/")


def cmd_screen(args, cfg: dict) -> None:
    from kdaytrader.screener import screen

    sc = cfg.get("screener") or {}
    kis_client = None
    if (args.source or sc.get("source")) == "kis":
        from kdaytrader.data.kis import KISClient

        k = cfg["kis"]
        kis_client = KISClient(k["app_key"], k["app_secret"], k["account"], paper=bool(k.get("paper", True)))
    found = screen(args.source or sc.get("source", "naver"), args.limit or int(sc.get("limit", 15)), kis_client=kis_client)
    for c, n in found.items():
        print(c, n)
    if not found:
        print("스크리닝 결과 없음 (네트워크 또는 API 설정 확인)")


def cmd_news(args, cfg: dict) -> None:
    from kdaytrader.data.news import IndexCollector, NewsCollector

    ctx = build_context(cfg, cfg["watchlist"])
    idx = IndexCollector(ctx)
    idx.fetch_once()
    nc = NewsCollector(ctx, stock_queries=cfg["watchlist"] if args.stocks else {})
    n = nc.fetch_once()
    print(f"뉴스 {n}건 수집" + (f" (마지막 오류: {nc.last_error})" if nc.last_error else ""))
    for name, (ts, val, chg) in ctx.index.items():
        print(f"{name} {val:,.2f} ({chg:+.2f}%)")
    print("\n[섹터 보드]")
    for s, rs, sent, cnt in ctx.sector_board():
        print(f"  {s:6s} RS {rs:+.2f}%p 뉴스 {sent:+.2f} ({cnt}건)")
    print("\n[최근 뉴스]")
    for item in ctx.recent_news(args.limit):
        tags = ",".join(item.sectors) + ("|거시" if item.macro else "") + ("|" + ",".join(item.codes) if item.codes else "")
        print(f"  {item.ts:%m-%d %H:%M} {item.sentiment:+.2f} [{tags}] {item.title}")
    now = now_kst()
    print("\n[종목별 컨텍스트 평가]")
    for code, name in cfg["watchlist"].items():
        a = ctx.assess(code, now)
        print(f"  {name or code:10s} bias {a.bias:+.1f} 차단={a.entry_block or '-'} 청산={a.exit_hint or '-'} | " + "; ".join(a.notes))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="한국주식 실시간 기술적 분석 데이트레이딩")
    ap.add_argument("-c", "--config", default="config.yaml", help="설정 파일 (기본 config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="실시간 매매/시그널 실행")
    p_run.add_argument("--feed", choices=["sim", "naver", "kis"])
    p_run.add_argument("--mode", choices=["paper", "live"])
    p_run.add_argument("--codes", help="쉼표로 구분한 종목코드 (설정 관심종목 대신 사용)")
    p_run.add_argument("--signal-only", action="store_true", help="주문 없이 시그널 알림만")
    p_run.add_argument("--no-dashboard", action="store_true")
    p_run.add_argument("--no-news", action="store_true", default=None, help="뉴스/지수 수집 비활성화")

    p_demo = sub.add_parser("demo", help="시뮬레이션 피드로 즉시 체험")
    p_demo.add_argument("--speed", type=float, default=120.0, help="재생 배속 (0=최대속도)")
    p_demo.add_argument("--codes")
    p_demo.add_argument("--signal-only", action="store_true")
    p_demo.add_argument("--no-dashboard", action="store_true")
    p_demo.add_argument("--no-news", action="store_true", default=None)
    p_demo.add_argument("--news", dest="no_news", action="store_false", help="데모에서도 실시간 뉴스/지수 수집")

    p_bt = sub.add_parser("backtest", help="분봉 백테스트")
    p_bt.add_argument("--feed", choices=["sim", "naver", "kis"])
    p_bt.add_argument("--codes")
    p_bt.add_argument("--days", type=int, default=10)
    p_bt.add_argument("--seed", type=int, default=1)
    p_bt.add_argument("--trades", action="store_true", help="개별 거래 출력")
    p_bt.add_argument("--csv-dir", help="ts,open,high,low,close,volume CSV 디렉터리 (파일명=종목코드)")
    p_bt.add_argument("--grid", help='파라미터 그리드 JSON 예: {"buy_threshold":[50,60],"atr_stop_mult":[1.2,1.8]}')
    p_bt.add_argument("--out", help="결과 저장 디렉터리")

    p_sc = sub.add_parser("screen", help="거래량 상위 종목 스크리닝")
    p_sc.add_argument("--source", choices=["naver", "kis"])
    p_sc.add_argument("--limit", type=int)

    p_news = sub.add_parser("news", help="거시/섹터/종목 뉴스 감성과 컨텍스트 평가 출력")
    p_news.add_argument("--limit", type=int, default=25)
    p_news.add_argument("--stocks", action="store_true", help="관심종목별 구글뉴스 검색 포함")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    {"run": cmd_run, "demo": cmd_demo, "backtest": cmd_backtest, "screen": cmd_screen, "news": cmd_news}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
