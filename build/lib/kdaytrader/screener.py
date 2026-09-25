"""장중 종목 스크리너: 거래량/거래대금 상위 + 가격대·등락률 필터로 당일 매매 유니버스를 만든다."""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# ETF/ETN/레버리지 등 데이트레이딩 유니버스에서 제외할 상품명 패턴
_ETF_RE = re.compile(r"(KODEX|TIGER|KBSTAR|ARIRANG|HANARO|SOL |ACE |KOSEF|TIMEFOLIO|PLUS |RISE |1Q |ETN|레버리지|인버스|선물|채권|WOORI|삼성 ?ETN|미래에셋 ?ETN)", re.I)


def is_fund_like(name: str) -> bool:
    return bool(name) and bool(_ETF_RE.search(name))


def screen(
    source: str = "naver",
    limit: int = 15,
    min_price: float = 2_000,
    max_price: float = 500_000,
    max_abs_change: float = 15.0,
    exclude_codes: set[str] | None = None,
    kis_client=None,
    markets: tuple[str, ...] = ("KOSPI", "KOSDAQ"),
) -> dict[str, str]:
    """{코드: 종목명} 반환. 실패 시 빈 dict."""
    exclude = exclude_codes or set()
    out: dict[str, str] = {}
    if source == "kis" and kis_client is not None:
        try:
            rows = kis_client.volume_rank(limit * 2, int(min_price), int(max_price))
        except Exception as e:
            log.warning("KIS 거래량 순위 조회 실패: %s", e)
            rows = []
        for r in rows:
            if r["code"] in exclude or abs(r["change_pct"]) > max_abs_change or is_fund_like(r["name"]):
                continue
            out[r["code"]] = r["name"]
            if len(out) >= limit:
                break
        return out
    from .data.naver import NaverFeed, top_volume_codes

    feed = NaverFeed()
    per_market = max(limit, 10)
    for m in markets:
        try:
            cands = top_volume_codes(per_market * 2, m)
        except Exception as e:
            log.warning("네이버 거래량 상위 조회 실패(%s): %s", m, e)
            continue
        for code, name in cands:
            if code in exclude or code in out or is_fund_like(name):
                continue
            tick = feed.fetch_quote(code)
            if tick is not None and is_fund_like(tick.name):
                continue
            if tick is None or not (min_price <= tick.price <= max_price) or abs(tick.change_pct) > max_abs_change:
                continue
            out[code] = name or tick.name
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return out
