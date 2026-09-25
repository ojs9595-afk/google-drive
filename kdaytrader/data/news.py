"""실시간 뉴스/지수 수집기: RSS(연합뉴스·한국경제·매일경제·구글뉴스) + 네이버/KIS 지수 폴링.

MarketContext 에 뉴스와 지수를 계속 밀어 넣는 비동기 태스크.
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from ..context import MarketContext
from ..market import KST

log = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

DEFAULT_RSS = [
    ("연합뉴스 경제", "https://www.yna.co.kr/rss/economy.xml"),
    ("연합뉴스 증권", "https://www.yna.co.kr/rss/stock.xml"),
    ("한국경제 금융", "https://www.hankyung.com/feed/finance"),
    ("한국경제 경제", "https://www.hankyung.com/feed/economy"),
    ("매일경제 경제", "https://www.mk.co.kr/rss/30100041/"),
    ("매일경제 증권", "https://www.mk.co.kr/rss/50200011/"),
]


def google_news_rss(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote(query)}+when:1d&hl=ko&gl=KR&ceid=KR:ko"


def parse_rss(text: str, source: str) -> list[tuple[datetime, str, str]]:
    """(ts, title, link) 리스트."""
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        ts = None
        if pub:
            try:
                ts = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                try:
                    ts = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                except ValueError:
                    ts = None
        if ts is None:
            ts = datetime.now(tz=KST)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(KST)
        if title:
            out.append((ts, title, link))
    return out


class NewsCollector:
    def __init__(self, context: MarketContext, rss: list[tuple[str, str]] | None = None, stock_queries: dict[str, str] | None = None, poll_sec: float = 60.0, session: requests.Session | None = None):
        self.ctx = context
        self.rss = list(rss if rss is not None else DEFAULT_RSS)
        for code, name in (stock_queries or {}).items():
            if name:
                self.rss.append((f"구글뉴스:{name}", google_news_rss(name)))
        self.poll_sec = poll_sec
        self.session = session or requests.Session()
        self.session.headers.update(_UA)
        self._running = False
        self.last_error: str = ""
        self.fetch_count = 0

    def fetch_once(self) -> int:
        added = 0
        for source, url in self.rss:
            try:
                r = self.session.get(url, timeout=8)
                r.raise_for_status()
            except Exception as e:  # pragma: no cover - network
                self.last_error = f"{source}: {e}"
                log.debug("RSS 실패 %s: %s", source, e)
                continue
            for ts, title, link in parse_rss(r.text, source):
                if self.ctx.add_news(ts, title, source, link) is not None:
                    added += 1
        self.fetch_count += 1
        return added

    async def run(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                n = await loop.run_in_executor(None, self.fetch_once)
                if n:
                    log.info("뉴스 %d건 수집", n)
            except Exception as e:  # pragma: no cover
                log.warning("뉴스 수집 오류: %s", e)
            await asyncio.sleep(self.poll_sec)

    def stop(self) -> None:
        self._running = False


class IndexCollector:
    """코스피/코스닥 지수 폴링 (네이버 비공식 API, 실패 시 KIS 클라이언트 사용)."""

    NAVER_INDEX = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ"}
    KIS_INDEX = {"KOSPI": "0001", "KOSDAQ": "1001"}

    def __init__(self, context: MarketContext, poll_sec: float = 5.0, kis_client=None, session: requests.Session | None = None):
        self.ctx = context
        self.poll_sec = poll_sec
        self.kis = kis_client
        self.session = session or requests.Session()
        self.session.headers.update(_UA)
        self._running = False
        self.last_error = ""

    def _naver(self, name: str) -> tuple[float, float] | None:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/index/{self.NAVER_INDEX[name]}"
        r = self.session.get(url, timeout=5)
        r.raise_for_status()
        datas = r.json().get("datas") or []
        if not datas:
            return None
        d = datas[0]
        val = float(str(d.get("closePrice", "0")).replace(",", ""))
        chg = float(str(d.get("fluctuationsRatio", "0")).replace(",", ""))
        code = str(d.get("compareToPreviousPrice", {}).get("code", ""))
        if code in ("4", "5") and chg > 0:
            chg = -chg
        return val, chg

    def _kis(self, name: str) -> tuple[float, float] | None:
        if self.kis is None:
            return None
        d = self.kis.get(
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            "FHPUP02100000",
            {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": self.KIS_INDEX[name]},
        ).get("output", {})
        if not d:
            return None
        return float(d.get("bstp_nmix_prpr", 0)), float(d.get("bstp_nmix_prdy_ctrt", 0))

    def fetch_once(self) -> int:
        n = 0
        for name in ("KOSPI", "KOSDAQ"):
            res = None
            for fn in (self._naver, self._kis):
                try:
                    res = fn(name)
                    if res:
                        break
                except Exception as e:  # pragma: no cover - network
                    self.last_error = f"{name}: {e}"
            if res:
                self.ctx.update_index(name, res[0], res[1])
                n += 1
        return n

    async def run(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                await loop.run_in_executor(None, self.fetch_once)
            except Exception as e:  # pragma: no cover
                log.warning("지수 수집 오류: %s", e)
            await asyncio.sleep(self.poll_sec)

    def stop(self) -> None:
        self._running = False
