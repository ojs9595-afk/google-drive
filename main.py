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

from kdaytrader.config import build_pairs, build_quant_params, build_risk_params, build_rules, build_strategy_params, load_config
from kdaytrader.factory import ConfigError, build_context, build_engine, make_feed_and_broker, make_kis_client, resolve_watchlist
from kdaytrader.market import KST, now_kst
from kdaytrader.strategy.ensemble import EnsembleStrategy


def setup_logging(log_dir: str, quiet_console: bool) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(Path(log_dir) / f"kdaytrader_{now_kst():%Y%m%d}.log", encoding="utf-8")]
    if not quiet_console:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def _watchlist_from_args(cfg: dict, args, kis_client=None) -> dict[str, str]:
    return resolve_watchlist(cfg, getattr(args, "codes", None), kis_client)


def cmd_run(args, cfg: dict) -> None:
    mode = args.mode or cfg["mode"]
    feed_name = args.feed or cfg["feed"]
    if mode == "live" and not bool((cfg.get("kis") or {}).get("paper", True)):
        print("⚠ 실계좌 주문 모드입니다. 손실이 발생할 수 있습니다. 계속하려면 'yes' 입력:", end=" ")
        if input().strip().lower() != "yes":
            raise SystemExit("취소")
    setup_logging(cfg["log_dir"], quiet_console=not args.no_dashboard)
    web_on = cfg["web"].get("enabled", True) and not args.no_web
    try:
        engine = build_engine(
            cfg, feed_name, mode, codes=args.codes,
            auto_trade=(False if args.signal_only else None), use_dashboard=not args.no_dashboard, use_news=not args.no_news,
            web_host=(cfg["web"].get("host", "127.0.0.1") if web_on else ""), web_port=int(args.port or cfg["web"].get("port", 8787)),
        )
    except ConfigError as e:
        raise SystemExit(f"설정 오류: {e}")
    if engine.web is not None:
        print(f"웹 대시보드: {engine.web.url}  (브라우저에서 열어 주세요)")
        if args.open_browser:
            import socket
            import threading
            import time as _t
            import webbrowser

            def _open_when_ready(host: str, port: int, url: str) -> None:
                for _ in range(60):  # 서버가 뜰 때까지 최대 30초 대기
                    try:
                        with socket.create_connection((host, port), 0.5):
                            break
                    except OSError:
                        _t.sleep(0.5)
                webbrowser.open(url)

            threading.Thread(target=_open_when_ready, args=(engine.web.host, engine.web.port, engine.web.url), daemon=True).start()
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
    watchlist = _watchlist_from_args(cfg, args)
    feed_name = args.feed or ("sim" if cfg["feed"] == "sim" else cfg["feed"])
    data = {}
    if feed_name == "sim":
        from kdaytrader.data.sim import DEFAULT_BASE_PRICES, generate_history

        for i, code in enumerate(watchlist):
            data[code] = generate_history(code, args.days, DEFAULT_BASE_PRICES.get(code, 50_000), seed=(args.seed or 0) + i * 100)
    else:
        try:
            feed, _, _ = make_feed_and_broker(cfg, feed_name, "paper")
        except ConfigError as e:
            raise SystemExit(f"설정 오류: {e}")
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
    bt = Backtester(EnsembleStrategy(sp), rp, float(cfg["initial_cash"]), interval_min=int(cfg["interval_min"]), quant_params=build_quant_params(cfg), pairs=build_pairs(cfg), rules=build_rules(cfg))
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
        try:
            kis_client = make_kis_client(cfg)
        except ConfigError as e:
            raise SystemExit(f"설정 오류: {e}")
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


def cmd_analyze(args, cfg: dict) -> None:
    import pandas as pd

    from kdaytrader import analytics
    from kdaytrader.data.base import candles_to_df

    watchlist = _watchlist_from_args(cfg, args)
    feed_name = args.feed or ("sim" if cfg["feed"] == "sim" else cfg["feed"])
    if feed_name == "sim":
        from kdaytrader.data.sim import SimFeed

        feed = SimFeed(history_days=args.days, seed=1)
    else:
        try:
            feed, _, _ = make_feed_and_broker(cfg, feed_name, "paper")
        except ConfigError as e:
            raise SystemExit(f"설정 오류: {e}")
    feed.subscribe(list(watchlist.keys()))
    loaded: dict[str, pd.DataFrame] = {}
    for code in watchlist:
        try:
            loaded[code] = candles_to_df(feed.minute_candles(code, args.days * 390))
        except Exception as e:
            print(f"{code} 로드 실패: {e}")
    bench = None
    if hasattr(feed, "index_minute_candles"):
        try:
            b = candles_to_df(feed.index_minute_candles("KOSPI", args.days * 390))["close"]
            bench = b if len(b) >= 60 else None
        except Exception as e:
            print(f"코스피 분봉 로드 실패: {e}")
    rows = []
    for code, name in watchlist.items():
        df = loaded.get(code)
        if df is None or len(df) < 60:
            continue
        c = df["close"]
        row = {
            "종목": name or code,
            "현재가": round(float(c.iloc[-1])),
            "실현변동성(%)": round(float(analytics.volatility(c, 120).dropna().iloc[-1]), 1),
            "지수가중변동성(%)": round(float(analytics.exponential_volatility(c, 0.9).dropna().iloc[-1]), 1),
            "z-score(60봉)": round(float(analytics.zscores(c, 60).iloc[-1]), 2),
            "최대낙폭(%)": round(float(analytics.max_drawdown(c).iloc[-1] * 100), 2),
            "낙폭지속(봉)": analytics.drawdown_duration(c),
            "연속상승(봉)": analytics.consecutive(c, True),
        }
        if bench is not None:
            b = analytics.beta(c, bench, 120).dropna()
            corr = analytics.correlation(c, bench, 120).dropna()
            row["β(코스피,120봉)"] = round(float(b.iloc[-1]), 2) if not b.empty else None
            row["상관(120봉)"] = round(float(corr.iloc[-1]), 2) if not corr.empty else None
            study = analytics.event_study(c, bench, args.event_threshold / 100.0, 10, "down")
            summ = analytics.event_response_summary(study)
            row["지수급락 이벤트"] = summ.get("events", 0)
            row["급락후 +5봉 평균(%)"] = summ.get("+5봉 평균(%)")
        rows.append(row)
    if not rows:
        raise SystemExit("분석할 데이터가 없습니다.")
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    print(pd.DataFrame(rows).to_string(index=False))
    if bench is not None and len(rows) >= 2:
        series = [df["close"] for df in loaded.values() if len(df) >= 60]
        if len(series) >= 2:
            basket, _ = analytics.backtest_basket(series, rebal_every=390)
            print(f"\n동일가중 바스켓(일 1회 리밸런스) 성과: {basket.iloc[-1] - 100:+.2f}%  |  코스피 같은 기간: {(bench.iloc[-1] / bench.iloc[0] - 1) * 100:+.2f}%")


def interactive_menu() -> list[str] | None:
    """인자 없이 실행하면 간단한 메뉴를 보여준다."""
    print("""
╔══════════════════════════════════════════════════╗
║   📈 K-DayTrader  한국주식 데이트레이딩 엔진       ║
╠══════════════════════════════════════════════════╣
║  1) 시뮬레이션 데모 (웹 대시보드, 네트워크 불필요) ║
║  2) 백테스트 (시뮬레이션 10일)                     ║
║  3) 실시간 시그널만 보기 (네이버, 주문 없음)       ║
║  4) 실시간 페이퍼 트레이딩 (네이버)                ║
║  5) 한국투자증권 모의/실전 (config.yaml 의 kis)    ║
║  6) 뉴스·거시 컨텍스트 평가                        ║
║  7) 거래량 상위 스크리닝                           ║
║  8) 시계열 분석 (변동성·베타·이벤트 스터디)        ║
║  0) 종료                                          ║
╚══════════════════════════════════════════════════╝""")
    choice = input("선택 > ").strip()
    table = {
        "1": ["demo", "--open-browser"],
        "2": ["backtest", "--feed", "sim", "--days", "10", "--trades"],
        "3": ["run", "--feed", "naver", "--signal-only", "--open-browser"],
        "4": ["run", "--feed", "naver", "--mode", "paper", "--open-browser"],
        "5": ["run", "--feed", "kis", "--mode", "live", "--open-browser"],
        "6": ["news", "--stocks"],
        "7": ["screen"],
        "8": ["analyze", "--feed", "sim"],
    }
    if choice in ("0", ""):
        return None
    if choice not in table:
        print("잘못된 선택입니다.")
        return None
    return table[choice]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="한국주식 실시간 기술적 분석 데이트레이딩")
    ap.add_argument("-c", "--config", default="config.yaml", help="설정 파일 (기본 config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=False)

    p_run = sub.add_parser("run", help="실시간 매매/시그널 실행")
    p_run.add_argument("--feed", choices=["sim", "naver", "kis"])
    p_run.add_argument("--mode", choices=["paper", "live"])
    p_run.add_argument("--codes", help="쉼표로 구분한 종목코드 (설정 관심종목 대신 사용)")
    p_run.add_argument("--signal-only", action="store_true", help="주문 없이 시그널 알림만")
    p_run.add_argument("--no-dashboard", action="store_true")
    p_run.add_argument("--no-news", action="store_true", default=None, help="뉴스/지수 수집 비활성화")
    p_run.add_argument("--no-web", action="store_true", help="웹 대시보드 비활성화")
    p_run.add_argument("--port", type=int, help="웹 대시보드 포트 (기본 8787)")
    p_run.add_argument("--open-browser", action="store_true", help="시작 시 브라우저 자동 열기")

    p_demo = sub.add_parser("demo", help="시뮬레이션 피드로 즉시 체험")
    p_demo.add_argument("--speed", type=float, default=120.0, help="재생 배속 (0=최대속도)")
    p_demo.add_argument("--codes")
    p_demo.add_argument("--signal-only", action="store_true")
    p_demo.add_argument("--no-dashboard", action="store_true")
    p_demo.add_argument("--no-news", action="store_true", default=None)
    p_demo.add_argument("--news", dest="no_news", action="store_false", help="데모에서도 실시간 뉴스/지수 수집")
    p_demo.add_argument("--no-web", action="store_true")
    p_demo.add_argument("--port", type=int)
    p_demo.add_argument("--open-browser", action="store_true")

    p_an = sub.add_parser("analyze", help="gs-quant 식 시계열 분석 (변동성·베타·낙폭·z-score·이벤트 스터디)")
    p_an.add_argument("--feed", choices=["sim", "naver", "kis"])
    p_an.add_argument("--codes")
    p_an.add_argument("--days", type=int, default=5)
    p_an.add_argument("--event-threshold", type=float, default=0.3, help="지수 1분 변동률(%%) 이벤트 기준")

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

    if argv is None and len(sys.argv) == 1:
        argv = interactive_menu()
        if argv is None:
            return
    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_help()
        return
    try:
        cfg = load_config(args.config)
        if cfg.get("_config_error"):
            print(f"⚠ 설정 파일 경고: {cfg['_config_error']} — 기본값으로 계속합니다.", file=sys.stderr)
        {"run": cmd_run, "demo": cmd_demo, "backtest": cmd_backtest, "screen": cmd_screen, "news": cmd_news, "analyze": cmd_analyze}[args.cmd](args, cfg)
    except (ConfigError, ValueError) as e:
        raise SystemExit(f"설정 오류: {e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"JSON 형식 오류 (--grid 등): {e}")


if __name__ == "__main__":
    main()
