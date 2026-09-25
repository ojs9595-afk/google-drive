"""네이버 금융 비공식 API 피드 (API 키 불필요, 폴링 기반 준실시간).

- 현재가: https://polling.finance.naver.com/api/realtime/domestic/stock/{code}
- 과거 분봉/일봉: https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=minute&count=N&requestType=0

비공식 엔드포인트이므로 예고 없이 바뀔 수 있다. 과도한 요청은 차단될 수 있으니 폴링 주기를 1초 이상으로 둔다.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import requests

from ..market import KST
from .base import Candle, DataFeed, Tick

log = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}


def _num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


class NaverFeed(DataFeed):
    name = "naver"

    def __init__(self, poll_interval: float = 1.5, session: requests.Session | None = None):
        super().__init__()
        self.poll_interval = poll_interval
        self.session = session or requests.Session()
        self.session.headers.update(_UA)
        self._names: dict[str, str] = {}

    # ----- 현재가 -----
    def fetch_quote(self, code: str) -> Tick | None:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
        try:
            r = self.session.get(url, timeout=5)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # pragma: no cover - network
            log.warning("naver quote %s 실패: %s", code, e)
            return None
        datas = data.get("datas") or []
        if not datas:
            return None
        d = datas[0]
        price = _num(d.get("closePrice"))
        if price <= 0:
            return None
        name = d.get("stockName", "")
        self._names[code] = name
        sign = -1.0 if str(d.get("compareToPreviousPrice", {}).get("code", "")) in ("4", "5") else 1.0
        change_pct = _num(d.get("fluctuationsRatio"))
        if change_pct > 0 and sign < 0:
            change_pct = -change_pct
        return Tick(
            code=code,
            ts=datetime.now(tz=KST),
            price=price,
            volume=0,
            acc_volume=int(_num(d.get("accumulatedTradingVolume"))),
            change_pct=change_pct,
            name=name,
        )

    def name_of(self, code: str) -> str:
        return self._names.get(code, "")

    # ----- 과거 캔들 -----
    def _fchart(self, code: str, timeframe: str, count: int) -> list[Candle]:
        url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe={timeframe}&count={count}&requestType=0"
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        return parse_fchart(r.text, timeframe)

    def minute_candles(self, code: str, count: int = 400, interval: int = 1) -> list[Candle]:
        candles = self._fchart(code, "minute", max(count * interval, 100))
        if interval > 1:
            candles = resample(candles, interval)
        return candles[-count:]

    def daily_candles(self, code: str, count: int = 60) -> list[Candle]:
        return self._fchart(code, "day", count)[-count:]

    # ----- 폴링 루프 -----
    async def run(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        last_key: dict[str, tuple] = {}
        while self._running:
            for code in list(self._codes):
                tick = await loop.run_in_executor(None, self.fetch_quote, code)
                if tick is None:
                    continue
                key = (tick.price, tick.acc_volume)
                if last_key.get(code) == key:
                    continue  # 변화 없음
                last_key[code] = key
                await self._emit(tick)
            await asyncio.sleep(self.poll_interval)


_ITEM_RE = re.compile(r'<item data="([^"]+)"')


def parse_fchart(xml_text: str, timeframe: str) -> list[Candle]:
    out: list[Candle] = []
    for m in _ITEM_RE.finditer(xml_text):
        parts = m.group(1).split("|")
        if len(parts) < 6:
            continue
        d = parts[0]
        try:
            if timeframe == "minute":
                ts = datetime.strptime(d, "%Y%m%d%H%M").replace(tzinfo=KST)
            else:
                ts = datetime.strptime(d, "%Y%m%d").replace(tzinfo=KST)
            o, h, lo, c = (float(x) for x in parts[1:5])
            v = int(float(parts[5])) if parts[5] not in ("", "null") else 0
        except ValueError:
            continue
        if c <= 0:
            continue
        # fchart 분봉의 시각은 캔들 '종료' 시각이므로 시작 시각으로 맞춘다.
        if timeframe == "minute":
            from datetime import timedelta

            ts = ts - timedelta(minutes=1)
        out.append(Candle(ts, o, h, lo, c, v))
    return out


def resample(candles: list[Candle], interval: int) -> list[Candle]:
    """1분봉 리스트를 N분봉으로 묶는다 (같은 날 내에서만)."""
    out: list[Candle] = []
    cur: Candle | None = None
    for c in candles:
        minute_of_day = c.ts.hour * 60 + c.ts.minute - 9 * 60
        bucket_min = (minute_of_day // interval) * interval + 9 * 60
        bucket = c.ts.replace(hour=bucket_min // 60, minute=bucket_min % 60, second=0, microsecond=0)
        if cur is None or bucket != cur.ts:
            if cur is not None:
                out.append(cur)
            cur = Candle(bucket, c.open, c.high, c.low, c.close, c.volume)
        else:
            cur.high = max(cur.high, c.high)
            cur.low = min(cur.low, c.low)
            cur.close = c.close
            cur.volume += c.volume
    if cur is not None:
        out.append(cur)
    return out


def top_volume_codes(limit: int = 30, market: str = "KOSPI") -> list[tuple[str, str]]:
    """네이버 거래량 상위 페이지에서 (코드, 종목명) 추출. market: KOSPI | KOSDAQ."""
    sosok = "0" if market.upper() == "KOSPI" else "1"
    url = f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}"
    r = requests.get(url, headers=_UA, timeout=10)
    r.encoding = "euc-kr"
    pat = re.compile(r'href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>')
    seen: list[tuple[str, str]] = []
    for code, name in pat.findall(r.text):
        if code not in [c for c, _ in seen]:
            seen.append((code, name.strip()))
        if len(seen) >= limit:
            break
    return seen
