"""한국투자증권(KIS) Open API 클라이언트: 인증, 시세 조회, 실시간 웹소켓, 주문.

- 실전: https://openapi.koreainvestment.com:9443 / ws://ops.koreainvestment.com:21000
- 모의: https://openapivts.koreainvestment.com:29443 / ws://ops.koreainvestment.com:31000

앱키/시크릿/계좌번호는 config.yaml 또는 환경변수(KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT)로 넣는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from ..market import KST
from .base import Candle, DataFeed, Tick

log = logging.getLogger(__name__)

REAL_BASE = "https://openapi.koreainvestment.com:9443"
PAPER_BASE = "https://openapivts.koreainvestment.com:29443"
REAL_WS = "ws://ops.koreainvestment.com:21000"
PAPER_WS = "ws://ops.koreainvestment.com:31000"


class KISClient:
    """REST 인증/조회/주문 공용 클라이언트."""

    def __init__(self, app_key: str, app_secret: str, account: str, paper: bool = True, token_cache: str | None = ".token_cache.json"):
        if not app_key or not app_secret:
            raise ValueError("KIS app_key/app_secret 이 필요합니다.")
        self.app_key = app_key
        self.app_secret = app_secret
        self.account = account.replace("-", "")
        self.paper = paper
        self.base = PAPER_BASE if paper else REAL_BASE
        self.ws_url = PAPER_WS if paper else REAL_WS
        self.session = requests.Session()
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._approval_key: str | None = None
        self.token_cache = Path(token_cache) if token_cache else None
        self._last_call = 0.0
        self.min_interval = 0.06 if not paper else 0.5  # 초당 호출 제한(실전 20회/모의 2회)

    # ----- 인증 -----
    def _load_cached_token(self) -> None:
        if not self.token_cache or not self.token_cache.exists():
            return
        try:
            d = json.loads(self.token_cache.read_text())
            if d.get("app_key") == self.app_key and d.get("expiry", 0) > _time.time() + 600:
                self._token = d["token"]
                self._token_expiry = d["expiry"]
        except Exception:
            pass

    def token(self) -> str:
        if self._token is None:
            self._load_cached_token()
        if self._token and _time.time() < self._token_expiry - 300:
            return self._token
        r = self.session.post(
            f"{self.base}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._token_expiry = _time.time() + int(d.get("expires_in", 86400))
        if self.token_cache:
            try:
                self.token_cache.write_text(json.dumps({"app_key": self.app_key, "token": self._token, "expiry": self._token_expiry}))
            except Exception:
                pass
        return self._token

    def approval_key(self) -> str:
        if self._approval_key:
            return self._approval_key
        r = self.session.post(
            f"{self.base}/oauth2/Approval",
            json={"grant_type": "client_credentials", "appkey": self.app_key, "secretkey": self.app_secret},
            timeout=10,
        )
        r.raise_for_status()
        self._approval_key = r.json()["approval_key"]
        return self._approval_key

    def _headers(self, tr_id: str, extra: dict | None = None) -> dict:
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if extra:
            h.update(extra)
        return h

    def _throttle(self) -> None:
        wait = self.min_interval - (_time.time() - self._last_call)
        if wait > 0:
            _time.sleep(wait)
        self._last_call = _time.time()

    def get(self, path: str, tr_id: str, params: dict) -> dict:
        self._throttle()
        r = self.session.get(f"{self.base}{path}", headers=self._headers(tr_id), params=params, timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("rt_cd") not in (None, "0"):
            raise RuntimeError(f"KIS {tr_id} 오류: {d.get('msg_cd')} {d.get('msg1')}")
        return d

    def post(self, path: str, tr_id: str, body: dict) -> dict:
        self._throttle()
        r = self.session.post(f"{self.base}{path}", headers=self._headers(tr_id), json=body, timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("rt_cd") not in (None, "0"):
            raise RuntimeError(f"KIS {tr_id} 오류: {d.get('msg_cd')} {d.get('msg1')}")
        return d

    # ----- 시세 -----
    def current_price(self, code: str) -> dict:
        d = self.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
        return d.get("output", {})

    def minute_candles(self, code: str, count: int = 400) -> list[Candle]:
        """당일 1분봉을 최신부터 30개씩 역방향 페이징으로 수집 (KIS 는 당일분만 제공)."""
        out: list[Candle] = []
        hour = datetime.now(tz=KST).strftime("%H%M%S")
        seen = set()
        while len(out) < count:
            d = self.get(
                "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                "FHKST03010200",
                {
                    "FID_ETC_CLS_CODE": "",
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_HOUR_1": hour,
                    "FID_PW_DATA_INCU_YN": "Y",
                },
            )
            rows = d.get("output2") or []
            if not rows:
                break
            new = 0
            for r in rows:
                key = (r["stck_bsop_date"], r["stck_cntg_hour"])
                if key in seen:
                    continue
                seen.add(key)
                new += 1
                ts = datetime.strptime(r["stck_bsop_date"] + r["stck_cntg_hour"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
                out.append(Candle(ts, float(r["stck_oprc"]), float(r["stck_hgpr"]), float(r["stck_lwpr"]), float(r["stck_prpr"]), int(r["cntg_vol"])))
            if new == 0:
                break
            oldest = min(rows, key=lambda r: r["stck_cntg_hour"])["stck_cntg_hour"]
            t = datetime.strptime(oldest, "%H%M%S") - timedelta(minutes=1)
            if t.hour < 9:
                break
            hour = t.strftime("%H%M%S")
        out.sort(key=lambda c: c.ts)
        return out[-count:]

    def daily_candles(self, code: str, count: int = 60) -> list[Candle]:
        end = datetime.now(tz=KST)
        start = end - timedelta(days=int(count * 1.6) + 10)
        d = self.get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        out = []
        for r in d.get("output2") or []:
            if not r.get("stck_bsop_date"):
                continue
            ts = datetime.strptime(r["stck_bsop_date"], "%Y%m%d").replace(tzinfo=KST)
            out.append(Candle(ts, float(r["stck_oprc"]), float(r["stck_hgpr"]), float(r["stck_lwpr"]), float(r["stck_clpr"]), int(r["acml_vol"])))
        out.sort(key=lambda c: c.ts)
        return out[-count:]

    def volume_rank(self, limit: int = 30, min_price: int = 1000, max_price: int = 1_000_000, min_volume: int = 100_000) -> list[dict]:
        """거래량 순위 (실전 계좌만 지원)."""
        d = self.get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                "FID_INPUT_PRICE_1": str(min_price),
                "FID_INPUT_PRICE_2": str(max_price),
                "FID_VOL_CNT": str(min_volume),
                "FID_INPUT_DATE_1": "",
            },
        )
        rows = d.get("output") or []
        return [
            {
                "code": r["mksc_shrn_iscd"],
                "name": r["hts_kor_isnm"],
                "price": float(r["stck_prpr"]),
                "change_pct": float(r["prdy_ctrt"]),
                "volume": int(r["acml_vol"]),
                "turnover": float(r.get("acml_tr_pbmn", 0)),
            }
            for r in rows[:limit]
        ]

    # ----- 주문/잔고 -----
    def _acct(self) -> tuple[str, str]:
        return self.account[:8], self.account[8:10] or "01"

    def order(self, code: str, qty: int, side: str, price: float = 0.0) -> dict:
        """시장가(price=0) 또는 지정가 주문. side: BUY | SELL."""
        cano, prdt = self._acct()
        if self.paper:
            tr_id = "VTTC0802U" if side == "BUY" else "VTTC0801U"
        else:
            tr_id = "TTTC0802U" if side == "BUY" else "TTTC0801U"
        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": prdt,
            "PDNO": code,
            "ORD_DVSN": "01" if price <= 0 else "00",
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": "0" if price <= 0 else str(int(price)),
        }
        return self.post("/uapi/domestic-stock/v1/trading/order-cash", tr_id, body)

    def balance(self) -> dict:
        cano, prdt = self._acct()
        tr_id = "VTTC8434R" if self.paper else "TTTC8434R"
        d = self.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id,
            {
                "CANO": cano,
                "ACNT_PRDT_CD": prdt,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        return d


# 실시간 체결(H0STCNT0) 필드 인덱스
_F_CODE, _F_TIME, _F_PRICE, _F_CHG_PCT, _F_ASK, _F_BID, _F_VOL, _F_ACC_VOL = 0, 1, 2, 5, 10, 11, 12, 13
_F_CTTR, _F_SELL_SUM, _F_BUY_SUM, _F_ASK_RSQN, _F_BID_RSQN = 18, 19, 20, 38, 39
_FIELDS_PER_RECORD = 46


def _fl(fields: list[str], idx: int) -> float:
    try:
        return float(fields[idx]) if idx < len(fields) and fields[idx] != "" else 0.0
    except ValueError:
        return 0.0


def parse_realtime(msg: str) -> list[Tick]:
    """'0|H0STCNT0|001|<fields^...>' 형식의 체결 메시지를 Tick 리스트로 변환."""
    parts = msg.split("|", 3)
    if len(parts) < 4 or parts[1] != "H0STCNT0":
        return []
    try:
        n = int(parts[2])
    except ValueError:
        n = 1
    fields = parts[3].split("^")
    ticks: list[Tick] = []
    today = datetime.now(tz=KST).strftime("%Y%m%d")
    for i in range(n):
        f = fields[i * _FIELDS_PER_RECORD : (i + 1) * _FIELDS_PER_RECORD]
        if len(f) < _F_ACC_VOL + 1:
            break
        try:
            ts = datetime.strptime(today + f[_F_TIME], "%Y%m%d%H%M%S").replace(tzinfo=KST)
            ticks.append(
                Tick(
                    code=f[_F_CODE],
                    ts=ts,
                    price=float(f[_F_PRICE]),
                    volume=int(f[_F_VOL]),
                    acc_volume=int(f[_F_ACC_VOL]),
                    change_pct=float(f[_F_CHG_PCT]),
                    ask=float(f[_F_ASK]),
                    bid=float(f[_F_BID]),
                    strength=_fl(f, _F_CTTR),
                    buy_vol=int(_fl(f, _F_BUY_SUM)),
                    sell_vol=int(_fl(f, _F_SELL_SUM)),
                    ask_qty=int(_fl(f, _F_ASK_RSQN)),
                    bid_qty=int(_fl(f, _F_BID_RSQN)),
                )
            )
        except (ValueError, IndexError):
            continue
    return ticks


class KISFeed(DataFeed):
    """KIS 웹소켓 실시간 체결 피드 + REST 과거 캔들."""

    name = "kis"

    def __init__(self, client: KISClient):
        super().__init__()
        self.client = client

    def minute_candles(self, code: str, count: int = 400, interval: int = 1) -> list[Candle]:
        candles = self.client.minute_candles(code, count * interval)
        if interval > 1:
            from .naver import resample

            candles = resample(candles, interval)
        return candles[-count:]

    def daily_candles(self, code: str, count: int = 60) -> list[Candle]:
        return self.client.daily_candles(code, count)

    async def run(self) -> None:
        import websockets

        self._running = True
        backoff = 1
        while self._running:
            try:
                key = self.client.approval_key()
                async with websockets.connect(self.client.ws_url, ping_interval=None) as ws:
                    for code in self._codes:
                        await ws.send(
                            json.dumps(
                                {
                                    "header": {"approval_key": key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
                                    "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
                                }
                            )
                        )
                    backoff = 1
                    while self._running:
                        msg = await ws.recv()
                        if not isinstance(msg, str):
                            continue
                        if msg.startswith("0|") or msg.startswith("1|"):
                            for tick in parse_realtime(msg):
                                await self._emit(tick)
                            continue
                        try:
                            d = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        tr = d.get("header", {}).get("tr_id")
                        if tr == "PINGPONG":
                            await ws.send(msg)
                        elif d.get("body", {}).get("rt_cd") not in (None, "0"):
                            log.warning("KIS WS 응답: %s", d.get("body", {}).get("msg1"))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - network
                if not self._running:
                    break
                log.warning("KIS 웹소켓 오류, %ss 후 재접속: %s", backoff, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
