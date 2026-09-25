"""리스크 관리: 포지션 사이징, 손절/익절/트레일링, 일일 손실 한도, 시간 제한."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .broker.base import Position
from .market import round_to_tick


@dataclass
class RiskParams:
    risk_per_trade: float = 0.01  # 계좌 대비 1회 거래 최대 손실 비율
    max_position_pct: float = 0.30  # 종목당 최대 투자 비중
    max_positions: int = 4
    atr_stop_mult: float = 1.5  # 손절 = 진입가 - ATR * mult
    min_stop_pct: float = 0.006  # 최소 손절폭 0.6%
    max_stop_pct: float = 0.03  # 최대 손절폭 3%
    take_profit_r: float = 2.0  # 익절 = 손절폭 * R
    trail_atr_mult: float = 1.2  # 트레일링 스탑 = 고점 - ATR * mult
    breakeven_r: float = 1.0  # 1R 수익 시 손절을 본전으로 이동
    partial_take_r: float = 1.5  # 1.5R 도달 시 절반 익절 (0 이면 비활성)
    partial_take_ratio: float = 0.5
    daily_loss_limit_pct: float = 0.03  # 일일 손실 한도 3% 도달 시 신규 진입 중단
    daily_profit_lock_pct: float = 0.0  # 일일 목표 수익 달성 시 중단 (0=비활성)
    max_trades_per_day: int = 30
    entry_start: time = time(9, 5)  # 개장 직후 5분은 노이즈가 커서 회피
    entry_end: time = time(14, 50)
    force_close: time = time(15, 12)  # 동시호가 전 강제 청산
    cooldown_after_loss_min: int = 10  # 같은 종목 손절 후 재진입 대기
    max_holding_min: int = 0  # 최대 보유 시간 (0=제한 없음)
    kelly_enabled: bool = True  # 최근 거래 성과 기반 분수 켈리 사이징
    kelly_lookback: int = 20
    kelly_fraction: float = 0.5  # 하프 켈리
    kelly_min_scale: float = 0.5
    kelly_max_scale: float = 1.5


@dataclass
class DailyStats:
    date: object = None
    start_equity: float = 0.0
    realized: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    last_loss_ts: dict[str, datetime] = field(default_factory=dict)
    halted_reason: str = ""
    pnl_history: list[float] = field(default_factory=list)  # 켈리 계산용 (일자 넘어도 유지)


class RiskManager:
    def __init__(self, params: RiskParams | None = None):
        self.p = params or RiskParams()
        self.stats = DailyStats()

    # ----- 일일 통계 -----
    def new_day(self, d, equity: float) -> None:
        hist = self.stats.pnl_history
        self.stats = DailyStats(date=d, start_equity=equity)
        self.stats.pnl_history = hist

    def ensure_day(self, now: datetime, equity: float) -> None:
        if self.stats.date != now.date():
            self.new_day(now.date(), equity)

    def record_trade(self, code: str, pnl: float, ts: datetime) -> None:
        s = self.stats
        s.realized += pnl
        s.trades += 1
        s.pnl_history.append(pnl)
        if len(s.pnl_history) > 200:
            s.pnl_history = s.pnl_history[-200:]
        if pnl > 0:
            s.wins += 1
        else:
            s.losses += 1
            s.last_loss_ts[code] = ts

    def daily_pnl_pct(self) -> float:
        if self.stats.start_equity <= 0:
            return 0.0
        return self.stats.realized / self.stats.start_equity * 100.0

    # ----- 진입 가능 여부 -----
    def can_enter(self, now: datetime, code: str, open_positions: int) -> tuple[bool, str]:
        p, s = self.p, self.stats
        t = now.time()
        if t < p.entry_start:
            return False, "장 초반 진입 대기"
        if t >= p.entry_end:
            return False, "신규 진입 시간 종료"
        if open_positions >= p.max_positions:
            return False, "최대 보유 종목 수 도달"
        if s.trades >= p.max_trades_per_day:
            return False, "일일 최대 거래 횟수 도달"
        if s.start_equity > 0:
            pct = s.realized / s.start_equity
            if pct <= -p.daily_loss_limit_pct:
                s.halted_reason = "일일 손실 한도 도달"
                return False, s.halted_reason
            if p.daily_profit_lock_pct > 0 and pct >= p.daily_profit_lock_pct:
                s.halted_reason = "일일 목표 수익 달성"
                return False, s.halted_reason
        last_loss = s.last_loss_ts.get(code)
        if last_loss is not None and now - last_loss < timedelta(minutes=p.cooldown_after_loss_min):
            return False, "손절 후 재진입 대기"
        return True, ""

    # ----- 손절폭 / 사이징 -----
    def stop_distance(self, price: float, atr: float) -> float:
        d = atr * self.p.atr_stop_mult if atr > 0 else price * self.p.min_stop_pct
        d = max(d, price * self.p.min_stop_pct)
        d = min(d, price * self.p.max_stop_pct)
        return d

    def kelly_scale(self) -> float:
        """최근 거래의 승률 W 와 손익비 R 로 f = W - (1-W)/R 을 구해 하프 켈리를 적용, 리스크 예산 배율로 변환."""
        p = self.p
        if not p.kelly_enabled:
            return 1.0
        hist = self.stats.pnl_history[-p.kelly_lookback:]
        if len(hist) < 10:
            return 1.0
        wins = [x for x in hist if x > 0]
        losses = [-x for x in hist if x <= 0]
        if not wins:
            return p.kelly_min_scale
        if not losses:
            return p.kelly_max_scale
        w = len(wins) / len(hist)
        r = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
        f = w - (1 - w) / r
        if f <= 0:
            return p.kelly_min_scale
        scale = (f * p.kelly_fraction) / p.risk_per_trade
        return max(p.kelly_min_scale, min(p.kelly_max_scale, scale))

    def position_size(self, equity: float, cash: float, price: float, atr: float, scale: float = 1.0) -> int:
        """scale: 변동성 타게팅/켈리 등 외부 배율 (0.5~1.5 권장)."""
        if price <= 0 or equity <= 0:
            return 0
        risk_amount = equity * self.p.risk_per_trade * max(0.1, scale)
        dist = self.stop_distance(price, atr)
        qty_by_risk = int(risk_amount // dist) if dist > 0 else 0
        qty_by_alloc = int((equity * self.p.max_position_pct) // price)
        qty_by_cash = int((cash * 0.995) // price)
        return max(0, min(qty_by_risk, qty_by_alloc, qty_by_cash))

    def initial_levels(self, price: float, atr: float) -> tuple[float, float, float]:
        dist = self.stop_distance(price, atr)
        stop = round_to_tick(price - dist, "down")
        tp = round_to_tick(price + dist * self.p.take_profit_r, "up")
        return stop, tp, dist

    def attach(self, pos: Position, atr: float) -> None:
        stop, tp, dist = self.initial_levels(pos.avg_price, atr)
        pos.stop_price = stop
        pos.take_profit = tp
        pos.risk_per_share = dist
        pos.atr_at_entry = atr
        pos.highest = max(pos.highest, pos.avg_price)

    # ----- 보유 중 관리 -----
    def update_trailing(self, pos: Position, price: float) -> str | None:
        """가격 갱신 시 트레일링/본전 이동. 변경이 있으면 설명 문자열 반환."""
        if price > pos.highest:
            pos.highest = price
        note = None
        r = pos.r_multiple(price)
        if not pos.breakeven_moved and self.p.breakeven_r > 0 and r >= self.p.breakeven_r:
            be = round_to_tick(pos.avg_price * (1 + 0.002), "up")  # 수수료 감안 살짝 위
            if be > pos.stop_price:
                pos.stop_price = be
                pos.breakeven_moved = True
                note = "손절가 본전 이동"
        atr = pos.atr_at_entry
        if atr > 0 and r >= 1.0:
            trail = round_to_tick(pos.highest - atr * self.p.trail_atr_mult, "down")
            if trail > pos.stop_price:
                pos.stop_price = trail
                note = f"트레일링 스탑 {trail:,}"
        return note

    def check_exit(self, pos: Position, price: float, now: datetime, low: float | None = None, high: float | None = None) -> tuple[str | None, float]:
        """청산 사유와 청산 가격. low/high 가 주어지면(캔들) 봉 내 도달 여부로 판단."""
        lo = low if low is not None else price
        hi = high if high is not None else price
        if now.time() >= self.p.force_close:
            return "장 마감 강제 청산", price
        if pos.stop_price > 0 and lo <= pos.stop_price:
            return "손절" if not pos.breakeven_moved else "본전/트레일링 스탑", min(pos.stop_price, price) if low is None else pos.stop_price
        if pos.take_profit > 0 and hi >= pos.take_profit:
            return "익절", max(pos.take_profit, price) if high is None else pos.take_profit
        if self.p.max_holding_min > 0 and now - pos.entry_ts >= timedelta(minutes=self.p.max_holding_min):
            return "최대 보유시간 초과", price
        return None, price

    def partial_take_qty(self, pos: Position, price: float) -> int:
        """부분 익절 수량 (아직 안 했고 partial_take_r 도달 시). 0 이면 없음."""
        if self.p.partial_take_r <= 0 or pos.partial_done:
            return 0
        if pos.r_multiple(price) >= self.p.partial_take_r and pos.qty >= 2:
            qty = int(math.floor(pos.qty * self.p.partial_take_ratio))
            if qty >= 1:
                pos.partial_done = True
                return qty
        return 0
