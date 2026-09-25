"""한국거래소(KRX) 장 운영 시간, 호가 단위, 수수료/세금 관련 유틸리티."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
except Exception:  # Windows 에 tzdata 가 없을 때: 한국은 서머타임이 없으므로 UTC+9 고정으로 충분
    KST = timezone(timedelta(hours=9), "KST")

MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)
# 15:20 ~ 15:30 은 동시호가(장 마감 단일가) 구간이므로 데이트레이딩 청산은 그 전에 끝내야 한다.
CLOSING_AUCTION_START = time(15, 20)


def now_kst() -> datetime:
    return datetime.now(tz=KST)


def to_kst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def is_market_open(dt: datetime | None = None) -> bool:
    """정규장(09:00~15:30) 여부. 공휴일은 별도 캘린더가 없으므로 주말만 제외한다."""
    dt = to_kst(dt or now_kst())
    if not is_weekday(dt.date()):
        return False
    return MARKET_OPEN <= dt.time() < MARKET_CLOSE


def seconds_until_open(dt: datetime | None = None) -> float:
    dt = to_kst(dt or now_kst())
    candidate = dt.replace(hour=9, minute=0, second=0, microsecond=0)
    if dt.time() >= MARKET_CLOSE or not is_weekday(dt.date()):
        candidate += timedelta(days=1)
    while not is_weekday(candidate.date()):
        candidate += timedelta(days=1)
    if candidate < dt:
        return 0.0
    return (candidate - dt).total_seconds()


def tick_size(price: float) -> int:
    """KRX 호가 단위 (2023-01 개편 기준, 코스피/코스닥 공통)."""
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def round_to_tick(price: float, direction: str = "nearest") -> int:
    """가격을 호가 단위에 맞춘다. direction: nearest | up | down."""
    if price <= 0:
        return 0
    ts = tick_size(price)
    if direction == "up":
        return int(math.ceil(price / ts) * ts)
    if direction == "down":
        return int(math.floor(price / ts) * ts)
    return int(round(price / ts) * ts)


class FeeModel:
    """매매 비용 모델. 기본값: 위탁수수료 0.015%, 매도 시 증권거래세+농특세 0.15% (2025년 기준)."""

    def __init__(self, commission_rate: float = 0.00015, tax_rate: float = 0.0015):
        self.commission_rate = commission_rate
        self.tax_rate = tax_rate

    def buy_cost(self, price: float, qty: int) -> float:
        return price * qty * self.commission_rate

    def sell_cost(self, price: float, qty: int) -> float:
        return price * qty * (self.commission_rate + self.tax_rate)

    def round_trip_rate(self) -> float:
        return self.commission_rate * 2 + self.tax_rate
