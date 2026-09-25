"""Rich 기반 터미널 대시보드."""
from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .market import now_kst
from .strategy.base import Action


def _pct_text(v: float, digits: int = 2) -> Text:
    color = "red" if v > 0 else ("blue" if v < 0 else "white")  # 한국식: 상승 빨강 / 하락 파랑
    return Text(f"{v:+.{digits}f}%", style=color)


def _won(v: float) -> Text:
    color = "red" if v > 0 else ("blue" if v < 0 else "white")
    return Text(f"{v:+,.0f}", style=color)


class Dashboard:
    def __init__(self, trader, feed_name: str, mode: str, context=None):
        self.trader = trader
        self.feed_name = feed_name
        self.mode = mode
        self.context = context
        self.started = now_kst()
        self.status = "시작 중"
        self.sim_time: datetime | None = None

    # ----- 패널 -----
    def header(self) -> Panel:
        t = self.trader
        equity = t.equity()
        broker = t.broker
        realized = broker.realized_pnl()
        unreal = sum(p.unrealized(t.store.last_price.get(c, p.avg_price)) for c, p in broker.positions().items())
        s = t.risk.stats
        clock = (self.sim_time or now_kst()).strftime("%Y-%m-%d %H:%M:%S")
        txt = Text.assemble(
            ("한국주식 데이트레이더  ", "bold"),
            (f"[{self.mode.upper()}/{self.feed_name}]  ", "cyan"),
            (f"{clock}  ", "white"),
            ("자산 ", "white"), (f"{equity:,.0f}원  ", "bold"),
            ("현금 ", "white"), (f"{broker.cash():,.0f}  ", ""),
            ("실현 ", "white"), _won(realized), ("  평가 ", "white"), _won(unreal),
            (f"  거래 {s.trades} (승 {s.wins}/패 {s.losses})  ", ""),
            (f"일간 {t.risk.daily_pnl_pct():+.2f}%  ", "bold"),
            (f"상태: {self.status}", "yellow"),
        )
        if s.halted_reason:
            txt.append(f"  ⚠ {s.halted_reason}", style="bold red")
        return Panel(txt, border_style="green")

    def watchlist(self) -> Panel:
        t = self.trader
        table = Table(expand=True, show_lines=False, pad_edge=False)
        for col, just in [("종목", "left"), ("코드", "left"), ("현재가", "right"), ("등락", "right"), ("VWAP", "right"), ("RSI", "right"), ("ADX", "right"), ("거래량비", "right"), ("국면", "center"), ("점수", "right"), ("신호", "center"), ("근거", "left")]:
            table.add_column(col, justify=just, no_wrap=col != "근거")
        for code in t.store.codes():
            price = t.store.last_price.get(code, 0.0)
            sig = t.state.last_signal.get(code)
            ind = t.state.last_indicators.get(code)
            tick = t.store.last_tick.get(code)
            chg = tick.change_pct if tick else 0.0
            vwap = f"{ind.vwap:,.0f}" if ind is not None else "-"
            rsi = f"{ind.rsi:.0f}" if ind is not None and ind.rsi == ind.rsi else "-"
            adx = f"{ind.adx:.0f}" if ind is not None and ind.adx == ind.adx else "-"
            vr = f"x{ind.vol_ratio:.1f}" if ind is not None and ind.vol_ratio == ind.vol_ratio else "-"
            score = sig.score if sig else 0.0
            score_style = "bold red" if score >= 55 else ("bold blue" if score <= -35 else "white")
            action = sig.action.value if sig else "-"
            act_style = {"BUY": "bold red", "SELL": "bold blue"}.get(action, "white")
            if code in t.broker.positions():
                action = f"보유 {action}" if action != "HOLD" else "보유"
                act_style = "bold yellow"
            regime = {"trend": "추세", "range": "횡보"}.get(sig.regime if sig else "", "-")
            reasons = ", ".join(sig.reasons[:2]) if sig else ""
            table.add_row(
                Text(t.name(code)), code, f"{price:,.0f}", _pct_text(chg), vwap, rsi, adx, vr, regime,
                Text(f"{score:+.0f}", style=score_style), Text(action, style=act_style), Text(reasons),
            )
        return Panel(table, title="관심종목 / 시그널", border_style="cyan")

    def positions(self) -> Panel:
        t = self.trader
        table = Table(expand=True, pad_edge=False)
        for col in ["종목", "수량", "평단", "현재가", "손익", "손익%", "R", "손절", "익절", "진입"]:
            table.add_column(col, justify="right" if col not in ("종목",) else "left")
        for code, p in t.broker.positions().items():
            price = t.store.last_price.get(code, p.avg_price)
            table.add_row(
                Text(t.name(code)), f"{p.qty:,}", f"{p.avg_price:,.0f}", f"{price:,.0f}", _won(p.unrealized(price)),
                _pct_text(p.unrealized_pct(price)), f"{p.r_multiple(price):+.1f}", f"{p.stop_price:,.0f}", f"{p.take_profit:,.0f}",
                p.entry_ts.strftime("%H:%M"),
            )
        if not t.broker.positions():
            table.add_row("(보유 없음)", *[""] * 9)
        return Panel(table, title="보유 포지션", border_style="yellow")

    def trades(self) -> Panel:
        t = self.trader
        table = Table(expand=True, pad_edge=False)
        for col in ["시각", "종목", "구분", "수량", "가격", "손익", "사유"]:
            table.add_column(col, justify="right" if col in ("수량", "가격", "손익") else "left", no_wrap=col != "사유")
        for ev in t.state.events[-12:][::-1]:
            side = Text(ev.side, style="bold red" if ev.side == "BUY" else "bold blue")
            table.add_row(ev.ts.strftime("%H:%M:%S"), Text(ev.name), side, f"{ev.qty:,}", f"{ev.price:,.0f}", _won(ev.pnl) if ev.pnl is not None else Text("-"), Text(ev.reason))
        return Panel(table, title="체결 / 시그널 로그", border_style="magenta")

    def market(self) -> Panel:
        ctx = self.context
        if ctx is None:
            return Panel(Text("컨텍스트 비활성"), title="시장 컨텍스트", border_style="white")
        lines = []
        for name, (ts, val, chg) in ctx.index.items():
            mom = ctx.index_momentum(name)
            lines.append(Text.assemble((f"{name} ", "bold"), (f"{val:,.2f} ", ""), _pct_text(chg), (f"  10분 ", ""), _pct_text(mom)))
        bias, txt = ctx.market_trend()
        lines.append(Text(f"시장 편향 {bias:+.2f}  {txt}", style="cyan"))
        board = ctx.sector_board()[:8]
        if board:
            tbl = Table(expand=True, pad_edge=False, show_header=True)
            for col in ["섹터", "RS", "뉴스감성", "건수"]:
                tbl.add_column(col, justify="right" if col != "섹터" else "left")
            for s, rs, sent, cnt in board:
                tbl.add_row(Text(s), _pct_text(rs), Text(f"{sent:+.2f}", style="red" if sent > 0.2 else ("blue" if sent < -0.2 else "white")), str(cnt))
            lines.append(tbl)
        return Panel(Group(*lines) if lines else Text("지수 데이터 대기 중"), title="거시 / 섹터", border_style="white")

    def news(self) -> Panel:
        ctx = self.context
        if ctx is None:
            return Panel(Text("-"), title="실시간 뉴스", border_style="white")
        table = Table(expand=True, pad_edge=False, show_header=False)
        table.add_column("시각", no_wrap=True)
        table.add_column("감성", no_wrap=True, justify="right")
        table.add_column("태그", no_wrap=True)
        table.add_column("제목")
        for n in ctx.recent_news(10):
            style = "red" if n.sentiment > 0.2 else ("blue" if n.sentiment < -0.2 else "white")
            tags = ",".join(n.sectors[:2]) + ("|거시" if n.macro else "")
            table.add_row(n.ts.strftime("%H:%M"), Text(f"{n.sentiment:+.1f}", style=style), Text(tags), Text(n.title[:70]))
        if not ctx.news:
            table.add_row("", "", "", "뉴스 수집 대기 중 (네트워크/RSS 설정 확인)")
        return Panel(table, title="실시간 뉴스 (거시·산업·종목)", border_style="white")

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self.header(), size=3, name="header"),
            Layout(name="body"),
            Layout(name="bottom", size=16),
        )
        layout["body"].split_row(Layout(self.watchlist(), ratio=3), Layout(self.market(), ratio=1))
        layout["bottom"].split_row(
            Layout(Group(self.positions(), self.trades()), ratio=2),
            Layout(self.news(), ratio=1),
        )
        return layout
