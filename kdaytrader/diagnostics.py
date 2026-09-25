"""인터넷 데이터 소스 연결 진단: 네이버 시세/차트/지수, RSS, 구글뉴스, 한국투자증권 토큰, 텔레그램."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .data.naver import _UA

CHECKS = [
    ("naver_quote", "네이버 실시간 시세", "https://polling.finance.naver.com/api/realtime/domestic/stock/005930"),
    ("naver_chart", "네이버 분봉 차트", "https://fchart.stock.naver.com/sise.nhn?symbol=005930&timeframe=minute&count=5&requestType=0"),
    ("naver_index", "네이버 코스피 지수", "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI"),
    ("naver_rank", "네이버 거래량 상위", "https://finance.naver.com/sise/sise_quant.naver"),
    ("rss_yna", "연합뉴스 RSS", "https://www.yna.co.kr/rss/economy.xml"),
    ("google_news", "구글뉴스 RSS", "https://news.google.com/rss/search?q=%EC%BD%94%EC%8A%A4%ED%94%BC&hl=ko&gl=KR&ceid=KR:ko"),
]


def _check_url(key: str, label: str, url: str, timeout: float = 8.0) -> dict:
    t0 = time.time()
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        ms = round((time.time() - t0) * 1000)
        ok = r.status_code == 200 and len(r.content) > 50
        detail = f"HTTP {r.status_code}, {len(r.content):,}B"
        if key == "naver_quote" and ok:
            try:
                d = r.json()["datas"][0]
                detail += f" · {d.get('stockName')} {d.get('closePrice')} ({d.get('marketStatus', '')})"
            except Exception:
                ok = False
                detail += " · JSON 형식이 예상과 다름 (API 변경?)"
        if key == "naver_chart" and ok and "<item" not in r.text:
            ok = False
            detail += " · XML 항목 없음"
        return {"key": key, "label": label, "ok": ok, "ms": ms, "detail": detail}
    except requests.exceptions.ProxyError as e:
        return {"key": key, "label": label, "ok": False, "ms": round((time.time() - t0) * 1000), "detail": f"프록시 오류: {str(e)[:120]}"}
    except requests.exceptions.SSLError as e:
        return {"key": key, "label": label, "ok": False, "ms": round((time.time() - t0) * 1000), "detail": f"SSL 오류 (사내 보안 프로그램/인증서 확인): {str(e)[:120]}"}
    except requests.exceptions.ConnectionError as e:
        return {"key": key, "label": label, "ok": False, "ms": round((time.time() - t0) * 1000), "detail": f"연결 실패 (인터넷/방화벽 확인): {str(e)[:120]}"}
    except Exception as e:
        return {"key": key, "label": label, "ok": False, "ms": round((time.time() - t0) * 1000), "detail": f"{type(e).__name__}: {str(e)[:120]}"}


def _check_kis(cfg: dict) -> dict:
    k = cfg.get("kis") or {}
    if not (k.get("app_key") and k.get("app_secret")):
        return {"key": "kis", "label": "한국투자증권 API", "ok": None, "ms": 0, "detail": "앱키 미설정 (설정 탭에서 입력)"}
    t0 = time.time()
    try:
        from .data.kis import KISClient

        c = KISClient(k["app_key"], k["app_secret"], k.get("account", ""), paper=bool(k.get("paper", True)))  # 토큰 캐시 공유 (1분당 1회 발급 제한)
        c.token()
        px = c.current_price("005930")
        return {"key": "kis", "label": "한국투자증권 API" + (" (모의)" if c.paper else " (실전)"), "ok": True, "ms": round((time.time() - t0) * 1000), "detail": f"토큰 OK · 삼성전자 {px.get('stck_prpr', '?')}"}
    except requests.exceptions.HTTPError as e:
        code = getattr(e.response, "status_code", 0)
        hint = " · 토큰 발급은 1분당 1회 제한 — 잠시 후 다시 시도" if code == 403 else ""
        return {"key": "kis", "label": "한국투자증권 API", "ok": False, "ms": round((time.time() - t0) * 1000), "detail": f"HTTP {code}{hint}: {str(e)[:120]}"}
    except Exception as e:
        return {"key": "kis", "label": "한국투자증권 API", "ok": False, "ms": round((time.time() - t0) * 1000), "detail": f"{type(e).__name__}: {str(e)[:160]}"}


def _check_telegram(cfg: dict) -> dict:
    t = cfg.get("telegram") or {}
    if not (t.get("token") and t.get("chat_id")):
        return {"key": "telegram", "label": "텔레그램 알림", "ok": None, "ms": 0, "detail": "미설정 (선택)"}
    t0 = time.time()
    try:
        r = requests.get(f"https://api.telegram.org/bot{t['token']}/getMe", timeout=8)
        ok = r.status_code == 200 and r.json().get("ok")
        return {"key": "telegram", "label": "텔레그램 알림", "ok": bool(ok), "ms": round((time.time() - t0) * 1000), "detail": ("봇 " + r.json()["result"].get("username", "")) if ok else f"HTTP {r.status_code}"}
    except Exception as e:
        return {"key": "telegram", "label": "텔레그램 알림", "ok": False, "ms": round((time.time() - t0) * 1000), "detail": f"{type(e).__name__}: {str(e)[:120]}"}


def run_diagnostics(cfg: dict) -> dict:
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_check_url, k, l, u) for k, l, u in CHECKS]
        futs.append(ex.submit(_check_kis, cfg))
        futs.append(ex.submit(_check_telegram, cfg))
        results = [f.result() for f in futs]
    n_ok = sum(1 for r in results if r["ok"] is True)
    n_fail = sum(1 for r in results if r["ok"] is False)
    core_ok = all(r["ok"] for r in results if r["key"] in ("naver_quote", "naver_chart"))
    hint = ""
    if not core_ok:
        hint = "네이버 시세에 연결되지 않습니다. 인터넷 연결, 회사 방화벽/프록시, 보안 프로그램의 HTTPS 검사 설정을 확인하세요. 시뮬레이션 모드는 인터넷 없이 동작합니다."
    return {"results": results, "ok": n_ok, "fail": n_fail, "core_ok": core_ok, "hint": hint, "checked_at": time.strftime("%H:%M:%S")}
