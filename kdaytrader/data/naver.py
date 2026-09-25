"""네이버 금융 비공식 API 피드 (API 키 불필요, 폴링 기반 준실시간).

- 현재가: https://polling.finance.naver.com/api/realtime/domestic/stock/{code}
- 과거 분봉/일봉: https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=minute&count=N&requestType=0

비공식 엔드포인트이므로 예고 없이 바뀔 수 있다. 과도한 요청은 차단될 수 있으니 폴링 주기를 1초 이상으로 둔다.
"""
from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

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

    def __init__(self, poll_interval: float = 1.5, session: requests.Session | None = None, max_workers: int = 6):
        super().__init__()
        self.poll_interval = max(0.5, float(poll_interval))
        self.session = session or requests.Session()
        self.session.headers.update(_UA)
        self._names: dict[str, str] = {}
        self.market_status: str = ""  # OPEN / CLOSE / PREOPEN 등 (네이버 응답)
        self.last_error: str = ""
        self.consecutive_failures = 0
        self.fetch_ok = 0
        self.fetch_fail = 0
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="naver")

    # ----- 현재가 -----
    def _quote_polling(self, code: str) -> dict | None:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
        r = self.session.get(url, timeout=5)
        r.raise_for_status()
        datas = r.json().get("datas") or []
        return datas[0] if datas else None

    def _quote_mobile(self, code: str) -> dict | None:
        """대체 엔드포인트 (모바일 API). 필드명이 polling 과 대체로 같다."""
        url = f"https://m.stock.naver.com/api/stock/{code}/basic"
        r = self.session.get(url, timeout=5)
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, dict) and d.get("closePrice") else None

    def fetch_quote(self, code: str) -> Tick | None:
        d = None
        err = None
        for fn in (self._quote_polling, self._quote_mobile):
            try:
                d = fn(code)
                if d:
                    break
            except Exception as e:  # pragma: no cover - network
                err = e
        if not d:
            self.fetch_fail += 1
            self.consecutive_failures += 1
            if err is not None:
                self.last_error = f"{code}: {type(err).__name__}: {str(err)[:100]}"
                if self.consecutive_failures in (1, 10, 100) or self.consecutive_failures % 500 == 0:
                    log.warning("네이버 시세 조회 실패(%d회 연속) %s", self.consecutive_failures, self.last_error)
            return None
        price = _num(d.get("closePrice"))
        if price <= 0:
            return None
        self.fetch_ok += 1
        self.consecutive_failures = 0
        name = d.get("stockName", "") or d.get("stockName2", "")
        if name:
            self._names[code] = name
        ms = d.get("marketStatus")
        if ms:
            self.market_status = str(ms)
        sign_code = str((d.get("compareToPreviousPrice") or {}).get("code", ""))
        change_pct = _num(d.get("fluctuationsRatio"))
        if sign_code in ("4", "5") and change_pct > 0:
            change_pct = -change_pct
        ts = datetime.now(tz=KST)
        traded = d.get("localTradedAt")
        if traded:
            try:
                ts = datetime.fromisoformat(str(traded)).astimezone(KST)
            except ValueError:
                pass
        return Tick(
            code=code,
            ts=ts,
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

    def index_minute_candles(self, name: str = "KOSPI", count: int = 400) -> list[Candle]:
        """코스피/코스닥 지수 분봉 (fchart symbol=KOSPI|KOSDAQ)."""
        return self._fchart(name.upper(), "minute", max(count, 100))[-count:]

    # ----- 폴링 루프 -----
    async def run(self) -> None:
        self._running = True
        workers = min(16, max(6, len(self._codes) // 6))
        if getattr(self._pool, "_shutdown", False) or getattr(self._pool, "_max_workers", 0) < workers:
            self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="naver")
        loop = asyncio.get_running_loop()
        last_key: dict[str, tuple] = {}
        last_emit: dict[str, datetime] = {}
        while self._running:
            started = loop.time()
            codes = list(self._codes)
            try:
                ticks = await loop.run_in_executor(None, lambda: list(self._pool.map(self.fetch_quote, codes)))
            except Exception as e:  # pragma: no cover
                log.warning("네이버 폴링 오류: %s", e)
                ticks = []
            now = datetime.now(tz=KST)
            for tick in ticks:
                if tick is None or not self._running:
                    continue
                try:
                    key = (tick.price, tick.acc_volume)
                    # 변화가 없어도 30초마다 한 번은 흘려보내 캔들이 닫히도록 한다
                    if last_key.get(tick.code) == key and now - last_emit.get(tick.code, now - timedelta(days=1)) < timedelta(seconds=30):
                        continue
                    last_key[tick.code] = key
                    last_emit[tick.code] = now
                    # 폴링 시각이 체결 시각보다 뒤이므로 캔들 집계는 현재 시각 기준으로 한다
                    tick.ts = now
                    await self._emit(tick)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # pragma: no cover
                    log.warning("틱 처리 오류 %s: %s", tick.code, e)
            elapsed = loop.time() - started
            await asyncio.sleep(max(0.2, self.poll_interval - elapsed))

    def stop(self) -> None:
        super().stop()
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


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


def top_marketcap_codes(limit: int = 50, market: str = "KOSPI") -> list[tuple[str, str]]:
    """네이버 시가총액 상위 페이지(50개/페이지)에서 (코드, 종목명) 추출."""
    sosok = "0" if market.upper() == "KOSPI" else "1"
    pat = re.compile(r'href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>')
    seen: list[tuple[str, str]] = []
    page = 1
    while len(seen) < limit and page <= 6:
        url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
        r = requests.get(url, headers=_UA, timeout=10)
        r.encoding = "euc-kr"
        found = 0
        for code, name in pat.findall(r.text):
            if code not in [c for c, _ in seen]:
                seen.append((code, name.strip()))
                found += 1
            if len(seen) >= limit:
                break
        if found == 0:
            break
        page += 1
    return seen[:limit]


def top_rise_codes(limit: int = 60, market: str = "KOSPI") -> list[tuple[str, str]]:
    """네이버 상승률 상위 페이지에서 (코드, 종목명) 추출."""
    sosok = "0" if market.upper() == "KOSPI" else "1"
    r = requests.get(f"https://finance.naver.com/sise/sise_rise.naver?sosok={sosok}", headers=_UA, timeout=10)
    r.encoding = "euc-kr"
    pat = re.compile(r'href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>')
    seen: list[tuple[str, str]] = []
    for code, name in pat.findall(r.text):
        if code not in [c for c, _ in seen]:
            seen.append((code, name.strip()))
        if len(seen) >= limit:
            break
    return seen


def search_stock(query: str) -> list[tuple[str, str]]:
    """종목명으로 (코드, 이름) 후보 검색 (네이버 자동완성, 비공식)."""
    url = f"https://ac.stock.naver.com/ac?q={requests.utils.quote(query)}&target=stock"
    r = requests.get(url, headers=_UA, timeout=6)
    r.raise_for_status()
    data = r.json()
    out: list[tuple[str, str]] = []
    items = data.get("items") if isinstance(data, dict) else data
    for it in items or []:
        # 형식이 바뀔 수 있어 관용적으로 파싱: 6자리 코드와 이름 문자열을 찾는다
        vals = []
        stack = [it]
        while stack:
            v = stack.pop()
            if isinstance(v, (list, tuple)):
                stack.extend(v)
            elif isinstance(v, dict):
                stack.extend(v.values())
            elif isinstance(v, str):
                vals.append(v)
        code = next((v for v in vals if re.fullmatch(r"\d{6}", v)), None)
        name = next((v for v in vals if v and not re.fullmatch(r"[\d.]+", v) and v != code and len(v) <= 30), None)
        if code and name:
            out.append((code, name))
    return out[:10]


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
