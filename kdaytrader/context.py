"""시장 컨텍스트: 거시 지수 흐름, 섹터 상대강도, 실시간 뉴스(거시/산업/종목) 감성, 예정 이벤트.

전략 점수에 대한 보정(bias), 신규 진입 차단(entry_block), 보유 종목 긴급 청산(exit_hint)을 산출한다.
네트워크가 없거나 데이터가 없으면 모두 중립(0 / 없음)으로 동작한다.
"""
from __future__ import annotations

import logging
import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .market import KST

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 섹터 사전 (종목 → 섹터, 섹터 → 뉴스 키워드)
# ---------------------------------------------------------------------------
DEFAULT_STOCK_SECTORS: dict[str, str] = {
    "005930": "반도체", "000660": "반도체", "042700": "반도체", "403870": "반도체", "058470": "반도체",
    "005380": "자동차", "000270": "자동차", "012330": "자동차", "204320": "자동차",
    "373220": "2차전지", "006400": "2차전지", "051910": "2차전지", "247540": "2차전지", "086520": "2차전지", "003670": "2차전지", "066970": "2차전지",
    "035420": "인터넷", "035720": "인터넷", "259960": "게임", "036570": "게임",
    "068270": "바이오", "207940": "바이오", "196170": "바이오", "000100": "바이오", "326030": "바이오", "028300": "바이오",
    "105560": "금융", "055550": "금융", "086790": "금융", "316140": "금융", "024110": "금융", "138040": "금융",
    "009540": "조선", "329180": "조선", "010140": "조선", "042660": "조선",
    "012450": "방산", "047810": "방산", "079550": "방산", "064350": "방산",
    "005490": "철강", "004020": "철강",
    "096770": "에너지", "010950": "에너지", "267250": "에너지", "015760": "에너지",
    "034020": "원전", "052690": "원전",
    "017670": "통신", "030200": "통신", "032640": "통신",
    "041510": "엔터", "352820": "엔터", "035900": "엔터", "122870": "엔터",
    "000720": "건설", "028260": "건설", "006360": "건설",
    "011200": "해운", "003490": "항공", "020560": "항공",
    "097950": "식품", "271560": "식품",
    "090430": "화장품", "002790": "화장품",
    "051900": "화장품",
}

SECTOR_KEYWORDS: dict[str, list[str]] = {
    "반도체": ["반도체", "HBM", "D램", "DRAM", "낸드", "파운드리", "엔비디아", "TSMC", "마이크론", "메모리", "AI 칩", "삼성전자", "SK하이닉스"],
    "자동차": ["자동차", "현대차", "기아", "전기차", "완성차", "자동차 관세", "테슬라"],
    "2차전지": ["2차전지", "이차전지", "배터리", "양극재", "리튬", "LG에너지솔루션", "에코프로", "삼성SDI", "ESS", "전고체"],
    "인터넷": ["네이버", "카카오", "플랫폼", "포털", "AI 서비스"],
    "게임": ["게임", "엔씨", "크래프톤", "넷마블"],
    "바이오": ["바이오", "제약", "임상", "FDA", "신약", "셀트리온", "삼성바이오", "알테오젠", "바이오시밀러"],
    "금융": ["금융", "은행", "증권", "보험", "금리", "KB금융", "신한", "하나금융", "배당", "밸류업"],
    "조선": ["조선", "선박", "HD현대", "한화오션", "삼성중공업", "LNG선"],
    "방산": ["방산", "방위", "K-방산", "한화에어로", "LIG넥스원", "현대로템", "수출 계약"],
    "철강": ["철강", "포스코", "현대제철", "철강 관세"],
    "에너지": ["정유", "유가", "원유", "OPEC", "SK이노베이션", "S-Oil", "한국전력", "전기요금"],
    "원전": ["원전", "원자력", "두산에너빌리티", "SMR"],
    "통신": ["통신", "SKT", "KT", "LG유플러스", "5G"],
    "엔터": ["엔터", "하이브", "JYP", "SM", "K팝", "아이돌"],
    "건설": ["건설", "부동산", "아파트", "분양", "PF"],
    "해운": ["해운", "컨테이너", "운임", "HMM"],
    "항공": ["항공", "대한항공", "여객"],
    "식품": ["식품", "라면", "K푸드"],
    "화장품": ["화장품", "K뷰티", "아모레"],
}

# ---------------------------------------------------------------------------
# 감성 사전 (한국어 금융 뉴스 헤드라인용)
# ---------------------------------------------------------------------------
POSITIVE_TERMS: dict[str, float] = {
    "급등": 1.0, "상승": 0.5, "강세": 0.6, "신고가": 0.9, "최고가": 0.8, "호재": 0.9, "호실적": 0.9, "어닝 서프라이즈": 1.0,
    "흑자전환": 0.9, "흑자 전환": 0.9, "실적 개선": 0.7, "사상 최대": 0.8, "역대 최대": 0.8, "수주": 0.7, "계약 체결": 0.6,
    "목표주가 상향": 0.8, "목표가 상향": 0.8, "매수 추천": 0.5, "투자의견 상향": 0.7, "자사주 매입": 0.6, "자사주 소각": 0.7,
    "배당 확대": 0.5, "승인": 0.5, "허가": 0.5, "돌파": 0.6, "반등": 0.5, "매수세": 0.4, "외국인 순매수": 0.5, "기관 순매수": 0.4,
    "성장": 0.3, "확대": 0.3, "호조": 0.5, "회복": 0.4, "금리 인하": 0.6, "규제 완화": 0.6, "관세 유예": 0.7, "관세 철회": 0.8,
    "합의": 0.4, "훈풍": 0.5, "랠리": 0.7, "상한가": 1.0, "급반등": 0.8, "낙관": 0.4, "기대감": 0.3, "수혜": 0.6, "특수": 0.4,
}
NEGATIVE_TERMS: dict[str, float] = {
    "급락": -1.0, "하락": -0.5, "약세": -0.6, "신저가": -0.9, "최저가": -0.8, "악재": -0.9, "어닝 쇼크": -1.0, "적자": -0.7,
    "적자 전환": -0.9, "적자전환": -0.9, "실적 부진": -0.7, "실적 악화": -0.8, "목표주가 하향": -0.8, "목표가 하향": -0.8,
    "투자의견 하향": -0.7, "매도 추천": -0.6, "유상증자": -0.7, "감자": -0.9, "거래정지": -1.0, "상장폐지": -1.0, "횡령": -1.0,
    "배임": -1.0, "소송": -0.5, "리콜": -0.7, "규제": -0.4, "제재": -0.6, "조사": -0.4, "압수수색": -0.8, "관세 부과": -0.7,
    "관세 인상": -0.8, "금리 인상": -0.6, "긴축": -0.5, "침체": -0.7, "위기": -0.6, "전쟁": -0.7, "공습": -0.7, "폭락": -1.0,
    "패닉": -0.9, "서킷브레이커": -1.0, "사이드카": -0.8, "매도세": -0.4, "외국인 순매도": -0.5, "기관 순매도": -0.4, "하한가": -1.0,
    "급감": -0.6, "감소": -0.3, "부진": -0.5, "우려": -0.4, "불확실성": -0.4, "경고": -0.4, "파산": -1.0, "디폴트": -0.9,
    "환율 급등": -0.6, "원화 약세": -0.4, "유가 급등": -0.4, "물가 급등": -0.5, "실업률 상승": -0.4, "공매도": -0.3, "차익 실현": -0.3,
}
NEGATION = ["없", "아니", "않", "부인", "해소", "완화", "우려 딛고", "딛고"]

MACRO_KEYWORDS = [
    "코스피", "코스닥", "증시", "환율", "금리", "FOMC", "연준", "한국은행", "금통위", "CPI", "물가", "관세", "무역", "수출",
    "경기", "GDP", "실업", "유가", "국채", "달러", "외국인", "글로벌", "미국 증시", "나스닥", "S&P", "다우", "지정학",
]


def sentiment_score(text: str) -> float:
    """헤드라인 감성 점수 -1 ~ +1 (사전 기반, 단순 부정어 처리)."""
    s = 0.0
    hits = 0
    for term, w in POSITIVE_TERMS.items():
        if term in text:
            neg = any(n in text[text.find(term) + len(term) : text.find(term) + len(term) + 6] for n in NEGATION)
            s += -w * 0.5 if neg else w
            hits += 1
    for term, w in NEGATIVE_TERMS.items():
        if term in text:
            neg = any(n in text[text.find(term) + len(term) : text.find(term) + len(term) + 6] for n in NEGATION)
            s += -w * 0.5 if neg else w
            hits += 1
    if hits == 0:
        return 0.0
    return max(-1.0, min(1.0, s / math.sqrt(hits)))


def detect_sectors(text: str) -> list[str]:
    return [sec for sec, kws in SECTOR_KEYWORDS.items() if any(k in text for k in kws)]


def is_macro(text: str) -> bool:
    return any(k in text for k in MACRO_KEYWORDS)


# ---------------------------------------------------------------------------
# 뉴스 아이템 / 예정 이벤트
# ---------------------------------------------------------------------------
@dataclass
class NewsItem:
    ts: datetime
    title: str
    source: str = ""
    link: str = ""
    sentiment: float = 0.0
    sectors: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    macro: bool = False

    def decayed(self, now: datetime, half_life_min: float) -> float:
        age = max(0.0, (now - self.ts).total_seconds() / 60.0)
        return self.sentiment * (0.5 ** (age / half_life_min))


@dataclass
class ScheduledEvent:
    """진입 회피 구간을 만드는 예정 이벤트 (예: 금통위 발표, 미국 CPI)."""

    name: str
    at: time  # KST
    before_min: int = 10
    after_min: int = 15
    date: object = None  # None 이면 매일

    def active(self, now: datetime) -> bool:
        if self.date is not None and now.date() != self.date:
            return False
        center = now.replace(hour=self.at.hour, minute=self.at.minute, second=0, microsecond=0)
        return center - timedelta(minutes=self.before_min) <= now <= center + timedelta(minutes=self.after_min)


@dataclass
class ContextAssessment:
    bias: float = 0.0
    entry_block: str = ""
    exit_hint: str = ""
    notes: list[str] = field(default_factory=list)
    market_bias: float = 0.0
    sector_bias: float = 0.0
    news_bias: float = 0.0


@dataclass
class ContextParams:
    news_half_life_min: float = 90.0
    stock_news_weight: float = 25.0  # 종목 뉴스 감성(-1~1) × 가중치 → 점수
    sector_news_weight: float = 12.0
    macro_news_weight: float = 10.0
    market_trend_weight: float = 15.0  # 지수 흐름(-1~1) × 가중치
    sector_rs_weight: float = 10.0  # 섹터 상대강도
    risk_off_index_drop_pct: float = -1.5  # 코스피 등락률 이하 → 신규 진입 금지
    risk_off_index_momentum_pct: float = -0.6  # 최근 10분 지수 변화율 이하 → 진입 금지
    stock_news_block: float = -0.45  # 종목 뉴스 감성 이하 → 진입 금지
    stock_news_exit: float = -0.7  # 종목 뉴스 감성 이하(신선한 뉴스) → 보유분 긴급 청산
    macro_shock: float = -0.7  # 거시 악재 감성 이하 → 전 종목 진입 금지
    urgent_news_age_min: float = 20.0


# ---------------------------------------------------------------------------
# MarketContext
# ---------------------------------------------------------------------------
class MarketContext:
    def __init__(
        self,
        params: ContextParams | None = None,
        stock_sectors: dict[str, str] | None = None,
        names: dict[str, str] | None = None,
        events: list[ScheduledEvent] | None = None,
    ):
        self.p = params or ContextParams()
        self.stock_sectors = dict(DEFAULT_STOCK_SECTORS)
        if stock_sectors:
            self.stock_sectors.update(stock_sectors)
        self.names = dict(names or {})
        self.events = list(events or [])
        self.news: deque[NewsItem] = deque(maxlen=500)
        self._seen_titles: set[str] = set()
        # 지수: 이름 → (ts, 값, 전일대비 %)
        self.index: dict[str, tuple[datetime, float, float]] = {}
        self._index_hist: dict[str, deque[tuple[datetime, float]]] = {}
        # 종목 등락률 (섹터 상대강도 계산용)
        self.stock_change: dict[str, float] = {}
        self.updated_at: datetime | None = None
        self.enabled = True

    # ----- 입력 -----
    def update_index(self, name: str, value: float, change_pct: float, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(tz=KST)
        self.index[name] = (ts, value, change_pct)
        h = self._index_hist.setdefault(name, deque(maxlen=5000))
        h.append((ts, value))
        self.updated_at = ts

    def update_stock_change(self, code: str, change_pct: float) -> None:
        self.stock_change[code] = change_pct

    def add_news(self, ts: datetime, title: str, source: str = "", link: str = "") -> NewsItem | None:
        key = re.sub(r"\s+", " ", title.strip())[:80]
        if key in self._seen_titles:
            return None
        self._seen_titles.add(key)
        if len(self._seen_titles) > 5000:
            self._seen_titles = set(list(self._seen_titles)[-2500:])
        codes = [c for c, n in self.names.items() if n and n in title]
        item = NewsItem(ts, title, source, link, sentiment_score(title), detect_sectors(title), codes, is_macro(title))
        self.news.append(item)
        self.updated_at = datetime.now(tz=KST)
        return item

    def index_series(self, name: str = "KOSPI"):
        """지수 이력을 1분 종가 시계열(pd.Series)로 반환. 없으면 None."""
        h = self._index_hist.get(name)
        if not h or len(h) < 2:
            return None
        import pandas as pd

        s = pd.Series([v for _, v in h], index=pd.DatetimeIndex([t for t, _ in h]))
        return s.resample("1min").last().dropna()

    def seed_index_history(self, name: str, candles) -> None:
        """과거 지수 분봉으로 이력을 채운다 (베타/이벤트 스터디용)."""
        h = self._index_hist.setdefault(name, deque(maxlen=5000))
        for c in candles:
            h.append((c.ts, c.close))
        if candles:
            last = candles[-1]
            self.index.setdefault(name, (last.ts, last.close, 0.0))

    # ----- 계산 -----
    def index_momentum(self, name: str, minutes: int = 10) -> float:
        h = self._index_hist.get(name)
        if not h or len(h) < 2:
            return 0.0
        now_ts, now_v = h[-1]
        past_v = None
        for ts, v in reversed(h):
            if now_ts - ts >= timedelta(minutes=minutes):
                past_v = v
                break
        if past_v is None:
            past_v = h[0][1]
        if past_v <= 0:
            return 0.0
        return (now_v / past_v - 1.0) * 100.0

    def market_trend(self) -> tuple[float, str]:
        """지수 기반 시장 편향 -1~1 과 설명."""
        if not self.index:
            return 0.0, ""
        ref = self.index.get("KOSPI") or next(iter(self.index.values()))
        _, _, chg = ref
        mom = self.index_momentum("KOSPI") if "KOSPI" in self.index else 0.0
        bias = max(-1.0, min(1.0, chg / 1.5 * 0.6 + mom / 0.5 * 0.4))
        return bias, f"코스피 {chg:+.2f}% (10분 {mom:+.2f}%)"

    def sector_of(self, code: str) -> str:
        return self.stock_sectors.get(code, "")

    def sector_relative_strength(self, sector: str) -> float:
        """섹터 평균 등락률 - 전체 관찰 종목 평균 등락률 (%p)."""
        if not sector or not self.stock_change:
            return 0.0
        all_vals = list(self.stock_change.values())
        sec_vals = [v for c, v in self.stock_change.items() if self.stock_sectors.get(c) == sector]
        if not sec_vals:
            return 0.0
        return sum(sec_vals) / len(sec_vals) - sum(all_vals) / len(all_vals)

    def news_sentiment(self, now: datetime, code: str = "", sector: str = "", macro_only: bool = False, max_age_min: float | None = None) -> tuple[float, int]:
        """조건에 맞는 뉴스의 시간 감쇠 가중 평균 감성과 건수."""
        vals = []
        for n in self.news:
            if max_age_min is not None and (now - n.ts).total_seconds() / 60.0 > max_age_min:
                continue
            if code and code not in n.codes:
                continue
            if sector and sector not in n.sectors:
                continue
            if macro_only and not n.macro:
                continue
            if n.sentiment == 0.0:
                continue
            vals.append(n.decayed(now, self.p.news_half_life_min))
        if not vals:
            return 0.0, 0
        # 강한 뉴스가 희석되지 않도록 절대값 가중 평균
        weights = [abs(v) for v in vals]
        return sum(v * w for v, w in zip(vals, weights)) / sum(weights), len(vals)

    def assess(self, code: str, now: datetime) -> ContextAssessment:
        a = ContextAssessment()
        if not self.enabled:
            return a
        p = self.p
        # 1) 거시: 지수 흐름
        mkt, mkt_txt = self.market_trend()
        a.market_bias = mkt
        if mkt_txt:
            a.notes.append(mkt_txt)
        if self.index:
            ref = self.index.get("KOSPI") or next(iter(self.index.values()))
            if ref[2] <= p.risk_off_index_drop_pct:
                a.entry_block = f"시장 급락(코스피 {ref[2]:+.2f}%)"
            elif "KOSPI" in self.index and self.index_momentum("KOSPI") <= p.risk_off_index_momentum_pct:
                a.entry_block = "지수 단기 급락"
        # 2) 거시 뉴스
        macro_s, macro_n = self.news_sentiment(now, macro_only=True)
        if macro_n:
            a.notes.append(f"거시뉴스 {macro_s:+.2f}({macro_n}건)")
            if macro_s <= p.macro_shock and not a.entry_block:
                a.entry_block = "거시 악재 뉴스"
        # 3) 섹터: 뉴스 + 상대강도
        sector = self.sector_of(code)
        sec_s, sec_n = self.news_sentiment(now, sector=sector) if sector else (0.0, 0)
        rs = self.sector_relative_strength(sector)
        if sector and (sec_n or rs):
            a.notes.append(f"{sector} 뉴스 {sec_s:+.2f}({sec_n}건) RS {rs:+.2f}%p")
        a.sector_bias = sec_s * p.sector_news_weight + max(-1.0, min(1.0, rs / 1.0)) * p.sector_rs_weight
        # 4) 종목 뉴스
        st_s, st_n = self.news_sentiment(now, code=code)
        fresh_s, fresh_n = self.news_sentiment(now, code=code, max_age_min=p.urgent_news_age_min)
        if st_n:
            a.notes.append(f"종목뉴스 {st_s:+.2f}({st_n}건)")
        a.news_bias = st_s * p.stock_news_weight + macro_s * p.macro_news_weight
        if st_s <= p.stock_news_block and not a.entry_block:
            a.entry_block = "종목 악재 뉴스"
        if fresh_n and fresh_s <= p.stock_news_exit:
            a.exit_hint = "종목 악재 속보"
        # 5) 예정 이벤트
        for ev in self.events:
            if ev.active(now):
                a.entry_block = a.entry_block or f"이벤트 대기({ev.name})"
                a.notes.append(f"이벤트 {ev.name} 구간")
                break
        a.bias = mkt * p.market_trend_weight + a.sector_bias + a.news_bias
        a.bias = max(-60.0, min(60.0, a.bias))
        return a

    def urgent_exit(self, code: str, now: datetime) -> str | None:
        """틱 단위로 확인하는 긴급 청산 사유 (신선한 종목 악재 / 시장 급락)."""
        if not self.enabled:
            return None
        fresh_s, fresh_n = self.news_sentiment(now, code=code, max_age_min=self.p.urgent_news_age_min)
        if fresh_n and fresh_s <= self.p.stock_news_exit:
            return "종목 악재 속보 청산"
        if "KOSPI" in self.index and self.index["KOSPI"][2] <= self.p.risk_off_index_drop_pct * 1.5:
            return "시장 급락 청산"
        return None

    def recent_news(self, n: int = 10) -> list[NewsItem]:
        return list(self.news)[-n:][::-1]

    def sector_board(self) -> list[tuple[str, float, float, int]]:
        """(섹터, 상대강도, 뉴스감성, 뉴스건수) 목록."""
        now = datetime.now(tz=KST)
        secs = sorted({s for s in self.stock_sectors.values()})
        out = []
        for s in secs:
            if not any(self.stock_sectors.get(c) == s for c in self.stock_change) and not any(s in n.sectors for n in self.news):
                continue
            sent, cnt = self.news_sentiment(now, sector=s)
            out.append((s, self.sector_relative_strength(s), sent, cnt))
        out.sort(key=lambda r: r[1], reverse=True)
        return out
