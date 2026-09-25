"""시뮬레이션 피드: 국면이 바뀌는 일중 랜덤워크 틱을 생성 (데모/테스트/백테스트용)."""
from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta

from ..market import KST, round_to_tick, tick_size
from .base import Candle, DataFeed, Tick


DEFAULT_BASE_PRICES = {
    "005930": 72_000,  # 삼성전자
    "000660": 190_000,  # SK하이닉스
    "035420": 210_000,  # NAVER
    "035720": 45_000,  # 카카오
    "005380": 240_000,  # 현대차
    "068270": 180_000,  # 셀트리온
    "373220": 400_000,  # LG에너지솔루션
    "000270": 110_000,  # 기아
}


class _Walker:
    """국면(추세/횡보)이 확률적으로 바뀌는 가격 생성기."""

    def __init__(self, price: float, rng: random.Random, vol_per_min: float = 0.0012):
        self.price = float(price)
        self.rng = rng
        self.vol = vol_per_min
        self.drift = 0.0
        self.regime_left = 0
        self.base_volume = max(100, int(2e9 / price))

    def next_minute(self, minute_of_day: int) -> tuple[float, float, float, float, int]:
        if self.regime_left <= 0:
            self.regime_left = self.rng.randint(15, 90)
            mode = self.rng.random()
            if mode < 0.3:
                self.drift = self.rng.uniform(0.0002, 0.0007)
            elif mode < 0.6:
                self.drift = -self.rng.uniform(0.0002, 0.0007)
            else:
                self.drift = 0.0
        self.regime_left -= 1
        # 개장·마감 근처 변동성/거래량 확대 (U자형)
        u = 1.0 + 0.8 * math.exp(-minute_of_day / 40.0) + 0.5 * math.exp(-(390 - minute_of_day) / 40.0)
        o = self.price
        path = [o]
        for _ in range(6):
            path.append(path[-1] * (1 + self.drift / 6 + self.rng.gauss(0, self.vol * u / math.sqrt(6))))
        c = path[-1]
        h = max(path)
        lo = min(path)
        self.price = c
        vol = int(self.base_volume * u * self.rng.lognormvariate(0, 0.6))
        return (
            round_to_tick(o) or 1,
            round_to_tick(h, "up") or 1,
            round_to_tick(lo, "down") or 1,
            round_to_tick(c) or 1,
            vol,
        )


def generate_day(code: str, day: datetime, base_price: float, seed: int | None = None, vol_per_min: float = 0.0012) -> list[Candle]:
    rng = random.Random(seed if seed is not None else hash((code, day.date())) & 0xFFFFFFFF)
    w = _Walker(base_price, rng, vol_per_min)
    start = day.replace(hour=9, minute=0, second=0, microsecond=0, tzinfo=KST)
    out = []
    for m in range(0, 390):  # 09:00 ~ 15:29
        o, h, lo, c, v = w.next_minute(m)
        out.append(Candle(start + timedelta(minutes=m), o, max(o, h, c), min(o, lo, c), c, v))
    return out


def generate_history(code: str, days: int, base_price: float, end: datetime | None = None, seed: int | None = None) -> list[Candle]:
    end = end or datetime.now(tz=KST)
    day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    days_list = []
    while len(days_list) < days:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            days_list.append(day)
    days_list.reverse()
    candles: list[Candle] = []
    price = base_price
    for i, d in enumerate(days_list):
        s = None if seed is None else seed + i
        day_c = generate_day(code, d, price, s)
        candles.extend(day_c)
        price = day_c[-1].close * (1 + random.Random(s).gauss(0, 0.004))  # 갭
    return candles


class SimFeed(DataFeed):
    """분봉을 여러 틱으로 쪼개 실시간처럼 흘려보낸다.

    speed: 1.0 이면 실제 시간(1분=60초), 60 이면 1분을 1초에 재생. 0 이면 대기 없이 즉시.
    """

    name = "sim"

    def __init__(self, base_prices: dict[str, float] | None = None, speed: float = 60.0, history_days: int = 3, seed: int | None = 42, ticks_per_minute: int = 4, start: datetime | None = None):
        super().__init__()
        self.base_prices = base_prices or DEFAULT_BASE_PRICES
        self.speed = speed
        self.history_days = history_days
        self.seed = seed
        self.ticks_per_minute = ticks_per_minute
        self._history: dict[str, list[Candle]] = {}
        self._today: dict[str, list[Candle]] = {}
        self.start = start or datetime.now(tz=KST).replace(hour=0, minute=0, second=0, microsecond=0)

    def _ensure(self, code: str) -> None:
        if code in self._history:
            return
        base = self.base_prices.get(code, 50_000)
        hist = generate_history(code, self.history_days, base, end=self.start, seed=self.seed)
        self._history[code] = hist
        last = hist[-1].close if hist else base
        self._today[code] = generate_day(code, self.start, last, (self.seed or 0) + 999)

    def minute_candles(self, code: str, count: int = 400, interval: int = 1) -> list[Candle]:
        self._ensure(code)
        return self._history[code][-count:]

    def today_candles(self, code: str) -> list[Candle]:
        self._ensure(code)
        return self._today[code]

    async def run(self) -> None:
        self._running = True
        for c in self._codes:
            self._ensure(c)
        n = 390
        acc = {c: 0 for c in self._codes}
        buy_acc = {c: 0 for c in self._codes}
        sell_acc = {c: 0 for c in self._codes}
        rng = random.Random((self.seed or 0) + 7)
        for m in range(n):
            if not self._running:
                break
            for k in range(self.ticks_per_minute):
                for code in self._codes:
                    candle = self._today[code][m]
                    path = [candle.open, candle.high, candle.low, candle.close]
                    if k == 0:
                        price = candle.open
                    elif k == self.ticks_per_minute - 1:
                        price = candle.close
                    else:
                        price = path[1 + (k % 2)]
                    vol = candle.volume // self.ticks_per_minute
                    acc[code] += vol
                    ts = candle.ts + timedelta(seconds=int(60 * k / self.ticks_per_minute))
                    # 캔들 방향과 상관된 체결강도/호가잔량 합성 (마이크로스트럭처 신호 테스트용)
                    rng_ = (candle.high - candle.low) or 1.0
                    body = (candle.close - candle.open) / rng_
                    strength = max(30.0, 100.0 + 60.0 * body + rng.gauss(0, 12))
                    buy_share = min(0.95, max(0.05, strength / (strength + 100.0)))
                    buy_acc[code] += int(vol * buy_share)
                    sell_acc[code] += vol - int(vol * buy_share)
                    base_q = max(1000, int(candle.volume * 3))
                    bid_q = int(base_q * (1 + 0.5 * body + rng.gauss(0, 0.15)))
                    ask_q = int(base_q * (1 - 0.5 * body + rng.gauss(0, 0.15)))
                    ts_ = tick_size(price)
                    prev_close = self._history[code][-1].close if self._history[code] else candle.open
                    await self._emit(
                        Tick(
                            code, ts, price, vol, acc[code], change_pct=(price / prev_close - 1) * 100,
                            ask=price + ts_, bid=price, strength=strength, buy_vol=buy_acc[code], sell_vol=sell_acc[code],
                            ask_qty=max(1, ask_q), bid_qty=max(1, bid_q),
                        )
                    )
                if self.speed > 0:
                    await asyncio.sleep(60.0 / self.ticks_per_minute / self.speed)
        self._running = False
