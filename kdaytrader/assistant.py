"""챗봇 도우미: 자연어 명령(관심종목 편집, 상위 종목 편입, 엔진 제어, 설정 변경, 조회)과 사용법 질문에 답한다.

- 규칙 기반 해석기: 인터넷/API 키 없이 동작. 한국어 패턴으로 의도를 찾아 AppController 의 기능을 호출한다.
- Claude 연동(선택): 설정의 assistant.api_key 가 있으면 같은 기능을 도구(tool)로 노출해 자유로운 표현도 처리한다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

STARTER_SUGGESTIONS = [
    "코스닥 거래량 상위 20개 관심종목에 추가해줘",
    "지금 추천 종목 알려줘",
    "시뮬레이션 시작해",
    "오늘 수익률 어때?",
    "손절 ATR 배수 2로 바꿔줘",
    "사용법 알려줘",
]

# 잘 알려진 종목명 → 코드 (네트워크 없이도 이름으로 추가할 수 있게)
KNOWN_STOCKS: dict[str, str] = {
    "삼성전자": "005930", "SK하이닉스": "000660", "하이닉스": "000660", "현대차": "005380", "현대자동차": "005380", "기아": "000270",
    "NAVER": "035420", "네이버": "035420", "카카오": "035720", "셀트리온": "068270", "LG에너지솔루션": "373220", "엘지에너지솔루션": "373220",
    "삼성SDI": "006400", "KB금융": "105560", "신한지주": "055550", "하나금융지주": "086790", "하나금융": "086790", "우리금융지주": "316140",
    "삼성바이오로직스": "207940", "삼성바이오": "207940", "LG화학": "051910", "포스코홀딩스": "005490", "POSCO홀딩스": "005490", "포스코": "005490",
    "현대모비스": "012330", "삼성물산": "028260", "삼성생명": "032830", "삼성화재": "000810", "SK이노베이션": "096770", "SK텔레콤": "017670", "SKT": "017670",
    "KT": "030200", "LG유플러스": "032640", "LG전자": "066570", "한국전력": "015760", "한전": "015760", "HD현대중공업": "329180", "현대중공업": "329180",
    "한화오션": "042660", "삼성중공업": "010140", "HD한국조선해양": "009540", "한화에어로스페이스": "012450", "한화에어로": "012450",
    "LIG넥스원": "079550", "현대로템": "064350", "두산에너빌리티": "034020", "기업은행": "024110", "메리츠금융지주": "138040",
    "에코프로": "086520", "에코프로비엠": "247540", "알테오젠": "196170", "HLB": "028300", "리가켐바이오": "141080", "레인보우로보틱스": "277810",
    "펄어비스": "263750", "카카오게임즈": "293490", "엔씨소프트": "036570", "크래프톤": "259960", "하이브": "352820", "JYP": "035900", "JYP엔터": "035900",
    "SM엔터": "041510", "에스엠": "041510", "HMM": "011200", "대한항공": "003490", "CJ제일제당": "097950", "아모레퍼시픽": "090430", "LG생활건강": "051900",
    "POSCO퓨처엠": "003670", "포스코퓨처엠": "003670", "삼성전기": "009150", "LG디스플레이": "034220", "한미반도체": "042700", "HPSP": "403870",
    "리노공업": "058470", "셀트리온제약": "068760", "유한양행": "000100", "SK바이오팜": "326030", "KODEX 200": "069500",
}

# 설정 항목의 한국어 표현 → 점 경로
SETTING_ALIASES: list[tuple[list[str], str, str]] = [
    (["매수 임계", "매수임계", "매수 기준 점수", "매수 점수"], "strategy.buy_threshold", "매수 임계 점수"),
    (["매도 임계", "매도임계", "매도 기준 점수"], "strategy.sell_threshold", "매도 임계 점수"),
    (["손절 atr", "손절 배수", "atr 배수", "손절폭"], "risk.atr_stop_mult", "손절 ATR 배수"),
    (["익절", "익절 r", "목표 r"], "risk.take_profit_r", "익절 R배수"),
    (["1회 손실", "회당 손실", "거래당 손실", "리스크 비율", "손실 비율"], "risk.risk_per_trade", "1회 거래 최대 손실 비율"),
    (["최대 종목", "동시 보유", "최대 보유 종목"], "risk.max_positions", "최대 동시 보유 종목 수"),
    (["종목당 비중", "최대 비중"], "risk.max_position_pct", "종목당 최대 비중"),
    (["일일 손실", "하루 손실", "일 손실 한도"], "risk.daily_loss_limit_pct", "일일 손실 한도"),
    (["진입 시작", "매수 시작 시각"], "risk.entry_start", "진입 시작 시각"),
    (["진입 종료", "매수 종료 시각"], "risk.entry_end", "진입 종료 시각"),
    (["강제 청산 시각", "청산 시각"], "risk.force_close", "강제 청산 시각"),
    (["트레일링"], "risk.trail_atr_mult", "트레일링 ATR 배수"),
    (["초기 자금", "초기자금", "시작 자금"], "initial_cash", "초기 자금"),
    (["캔들 주기", "봉 주기", "분봉"], "interval_min", "시그널 캔들 주기(분)"),
    (["폴링 주기"], "naver.poll_interval", "네이버 폴링 주기(초)"),
    (["배속"], "sim.speed", "시뮬레이션 배속"),
]

HELP_TOPICS: list[dict] = [
    {"keys": ["시작", "처음", "사용법", "어떻게 써", "어떻게 사용", "뭐부터", "가이드", "help", "도움"], "title": "처음 시작하기",
     "answer": "1) 홈에서 **체험하기**(시뮬레이션)로 흐름을 익히세요. 인터넷·계좌 없이 동작합니다.\n2) **설정**에서 관심종목과 리스크(1회 손실 %, 손절 배수)를 조정하세요. 챗봇에게 \"코스닥 상위 20개 추가\"처럼 말해도 됩니다.\n3) **백테스트**로 전략 성과를 확인하세요.\n4) 장중에 **실시간 시그널만** → **페이퍼 트레이딩**으로 검증하세요.\n5) 충분히 검증한 뒤 **한국투자증권 모의투자**로 넘어가세요."},
    {"keys": ["모드", "페이퍼", "시그널만", "라이브", "실계좌", "차이"], "title": "실행 모드 차이",
     "answer": "- **체험하기**: 시뮬레이션 시세, 가상 체결. 인터넷 불필요.\n- **실시간 시그널만**: 네이버 실시간 시세로 신호만 표시, 주문 없음.\n- **페이퍼 트레이딩**: 실시간 시세 + 가상 계좌 자동매매.\n- **한국투자증권**: KIS API 로 모의투자(kis.paper=true) 또는 실계좌 주문. 실계좌는 반드시 모의투자로 검증 후 사용하세요."},
    {"keys": ["kis", "한국투자", "한투", "앱키", "api 키", "계좌"], "title": "한국투자증권 연동",
     "answer": "1) https://apiportal.koreainvestment.com 에서 앱키·시크릿을 발급합니다 (모의투자 신청 권장).\n2) **설정 → 한국투자증권 API** 에 앱키, 시크릿, 계좌번호(예: 12345678-01)를 넣고 서버를 **모의투자**로 두고 저장합니다.\n3) 홈의 **연결 진단**으로 토큰 발급이 되는지 확인한 뒤 **KIS 시작**을 누르세요.\n토큰 발급은 1분에 1회 제한이 있습니다."},
    {"keys": ["점수", "시그널", "신호", "매수 신호", "매도 신호", "추천 기준", "임계"], "title": "시그널 점수와 매수/매도 기준",
     "answer": "각 종목은 1분 캔들이 닫힐 때마다 -100~+100 점수를 받습니다. 추세(VWAP/EMA), 모멘텀(MACD/RSI/스토캐스틱), 돌파(ORB/볼린저), 평균회귀, 슈퍼트렌드, 퀀트 알파(페어·마이크로스트럭처·모멘텀), 거시·섹터·뉴스 보정을 합산합니다.\n- **매수**: 점수 ≥ 매수 임계값(기본 55) + VWAP 위, RSI ≤ 78, 거래량·변동성 필터 통과\n- **매도/주의**: 점수 ≤ 매도 임계값(기본 -35). 보유 중이면 청산 검토\n임계값은 설정 또는 챗봇(\"매수 임계값 60으로\")으로 바꿀 수 있습니다."},
    {"keys": ["추천", "추천 종목", "관심 종목 추천"], "title": "추천 종목 페이지",
     "answer": "**추천 종목** 페이지는 관심종목(유니버스) 전체를 점수순으로 보여 줍니다. 매수 카드에는 제안 수량·손절가·목표가가 리스크 설정으로 계산되어 표시되고, 시그널 히스토리에는 임계값을 넘은 순간이 기록됩니다. 유니버스를 넓히려면 \"코스닥 거래량 상위 30개 추가\"처럼 요청하거나 설정의 스크리너를 켜세요."},
    {"keys": ["손절", "익절", "리스크", "위험", "수량", "포지션 크기", "트레일링", "본전"], "title": "리스크 관리 규칙",
     "answer": "- 손절: 진입가 − ATR×손절배수(기본 1.5, 최소 0.6%·최대 3%)\n- 익절: 손절폭 × R배수(기본 2)\n- 1R 수익 시 손절을 본전으로 이동, 이후 고점 − ATR×1.2 트레일링\n- 1.5R 에서 절반 부분 익절\n- 수량: 계좌 × 1회 손실 비율(기본 1%) ÷ 손절폭, 종목당 최대 30%\n- 일일 손실 한도(기본 3%) 도달 시 신규 진입 중단, 15:12 전량 청산\n설정 → 리스크에서 조정할 수 있습니다."},
    {"keys": ["백테스트", "과거", "검증"], "title": "백테스트",
     "answer": "**백테스트** 페이지에서 데이터(시뮬레이션/네이버 분봉/KIS)와 기간·종목을 고르고 실행하면 수익률, 승률, 손익비, 최대낙폭, 샤프/소르티노/칼마와 거래 내역을 보여 줍니다. 네이버 분봉은 최근 며칠치만 제공됩니다. 챗봇에서 \"5일 백테스트 돌려줘\"도 됩니다."},
    {"keys": ["수익률", "성과", "손익", "csv", "거래 내역"], "title": "수익률 관리",
     "answer": "**수익률 관리** 페이지는 logs/trades_날짜.csv 에 누적된 실거래·페이퍼 거래를 모아 일별/월별/종목별/사유별/시간대별 통계와 연승·연패, 낙폭을 보여 줍니다. 시뮬레이션 거래는 기본 제외이며 '시뮬레이션 포함'으로 볼 수 있습니다. CSV 다운로드 버튼으로 내보낼 수 있습니다."},
    {"keys": ["연결", "인터넷", "안 돼", "안돼", "실패", "오류", "에러", "진단", "접속"], "title": "연결 문제 해결",
     "answer": "홈의 **연결 진단**을 눌러 네이버 시세·차트·지수, RSS, KIS, 텔레그램 연결을 확인하세요. 실패하면 인터넷, 회사 방화벽/프록시, 보안 프로그램의 HTTPS 검사 설정을 점검하세요. 시뮬레이션은 인터넷 없이 동작합니다. 포트 8787 이 사용 중이면 다음 빈 포트로 자동 이동하며 주소는 콘솔에 표시됩니다."},
    {"keys": ["장 시간", "장시간", "몇 시", "개장", "마감", "장중"], "title": "장 운영 시간",
     "answer": "정규장은 평일 09:00~15:30 입니다. 기본 설정은 09:05~14:50 에만 신규 진입하고 15:12 에 전량 청산합니다(동시호가 15:20 이전). 장 밖에서는 실시간 시세가 멈춰 있어 표시만 하고 캔들/시그널은 만들지 않습니다."},
    {"keys": ["자동매매", "자동 매매", "끄", "켜", "수동"], "title": "자동매매 ON/OFF",
     "answer": "대시보드의 **자동매매** 버튼 또는 챗봇(\"자동매매 꺼줘\")으로 전환합니다. OFF 이면 신호만 알리고 주문·강제청산은 하지 않습니다. 보유분을 정리하려면 **전량 청산** 버튼을 누르세요."},
    {"keys": ["뉴스", "거시", "섹터", "코스피", "지수"], "title": "뉴스·거시 컨텍스트",
     "answer": "연합뉴스·한국경제·매일경제 RSS 와 종목별 구글뉴스를 60초마다, 코스피/코스닥 지수를 5초마다 수집해 감성 점수로 종목 점수를 보정합니다. 코스피 -1.5% 이하나 강한 악재 뉴스면 신규 진입을 막고, 종목 악재 속보면 보유분을 청산합니다. **뉴스·거시** 페이지에서 확인할 수 있습니다."},
    {"keys": ["텔레그램", "알림"], "title": "텔레그램 알림",
     "answer": "설정 → 알림에 봇 토큰과 채팅 ID 를 넣으면 매수/매도 시그널과 체결이 텔레그램으로 전송됩니다. @BotFather 로 봇을 만들고, 봇에게 메시지를 보낸 뒤 getUpdates 로 chat_id 를 확인하세요."},
    {"keys": ["관심종목", "종목 추가", "종목 삭제", "유니버스", "스크리너"], "title": "관심종목 편집",
     "answer": "설정 → 관심종목에서 코드로 추가/삭제하거나, 챗봇에게 \"삼성전자 추가\", \"카카오 빼줘\", \"코스닥 거래량 상위 50개 추가\", \"코스피 시가총액 상위 30개 추가\"처럼 말하면 됩니다. 실행 중이면 즉시 구독됩니다. 설정의 스크리너를 켜면 장중 N분마다 거래량 상위 종목이 자동 편입됩니다."},
    {"keys": ["챗봇", "도우미", "claude", "클로드", "ai"], "title": "챗봇 도우미",
     "answer": "이 도우미는 인터넷 없이 규칙으로 명령을 해석합니다. 설정 → assistant.api_key 에 Anthropic API 키를 넣으면 Claude 가 같은 기능을 도구로 사용해 더 자유로운 표현을 이해합니다. 할 수 있는 일: 관심종목 편집, 상위 종목 편입, 엔진 시작/정지, 자동매매 전환, 전량 청산, 설정 변경, 추천/보유/수익률/상태 조회, 백테스트, 뉴스 수집, 연결 진단, 사용법 안내."},
]


@dataclass
class ChatResult:
    reply: str
    actions: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    needs_confirm: dict | None = None
    data: dict | None = None
    source: str = "rules"

    def to_dict(self) -> dict:
        return {"reply": self.reply, "actions": self.actions, "suggestions": self.suggestions or STARTER_SUGGESTIONS[:4], "needs_confirm": self.needs_confirm, "data": self.data, "source": self.source}


def _won(v) -> str:
    try:
        return f"{float(v):+,.0f}원"
    except (TypeError, ValueError):
        return str(v)


class Assistant:
    def __init__(self, ctl):
        self.ctl = ctl
        self._llm = None

    # ======================= 진입점 =======================
    def handle(self, message: str, history: list[dict], confirm: bool = False) -> dict:
        text = (message or "").strip()
        if not text:
            return ChatResult("무엇을 도와드릴까요? 예: \"코스닥 거래량 상위 20개 추가\", \"시뮬레이션 시작\", \"사용법 알려줘\"").to_dict()
        cfg = self.ctl.cfg.get("assistant") or {}
        if cfg.get("api_key") and not confirm:
            try:
                res = self._handle_llm(text, history, cfg)
                if res is not None:
                    return res.to_dict()
            except Exception as e:
                log.warning("LLM 도우미 실패, 규칙 기반으로 전환: %s", e)
                res = self.rules(text, confirm)
                res.reply = f"(Claude 연동 오류: {str(e)[:120]} — 규칙 기반으로 처리했습니다)\n\n" + res.reply
                return res.to_dict()
        return self.rules(text, confirm).to_dict()

    # ======================= 규칙 기반 =======================
    def rules(self, text: str, confirm: bool = False) -> ChatResult:
        # "A 하고 B", "A 그리고 B", 줄바꿈으로 여러 명령을 나눈다 (쉼표는 종목 나열에 쓰이므로 분리하지 않음)
        parts = [p.strip() for p in re.split(r"\s*(?:그리고|그다음|그 다음|\n)\s*|(?<=하고)\s+", text) if p.strip()]
        results = [self._one(p, confirm) for p in parts] or [self._one(text, confirm)]
        if len(results) == 1:
            return results[0]
        reply = "\n\n".join(r.reply for r in results)
        actions = [a for r in results for a in r.actions]
        confirm_req = next((r.needs_confirm for r in results if r.needs_confirm), None)
        return ChatResult(reply, actions, results[-1].suggestions, confirm_req)

    def _one(self, t: str, confirm: bool) -> ChatResult:
        low = t.lower().replace(" ", "")
        # --- 관심종목: 상위 N개 ---
        m_top = re.search(r"(상위|top)\s*(\d+)", t, re.I) or re.search(r"(\d+)\s*(개|종목)", t)
        wants_add = any(k in low for k in ("추가", "넣", "편입", "담아", "포함", "등록"))
        wants_remove = any(k in low for k in ("빼", "삭제", "제거", "제외", "지워"))
        wants_watch = any(k in low for k in ("관심", "종목", "유니버스", "watch"))
        if m_top and wants_add and (wants_watch or "상위" in low or "top" in low):
            n = int(m_top.group(2) if m_top.re.pattern.startswith("(상위") else m_top.group(1))
            market = "KOSDAQ" if "코스닥" in low else ("KOSPI" if "코스피" in low else "ALL")
            by = "marketcap" if ("시가총액" in low or "시총" in low) else "volume"
            return self.tool_add_top(market, n, by)
        if wants_watch and any(k in low for k in ("전부", "모두", "다", "전체")) and any(k in low for k in ("삭제", "비워", "초기화", "지워")):
            return self.tool_clear_watchlist(confirm)
        if wants_remove and (wants_watch or self._extract_stocks(t)):
            names = self._extract_stocks(t)
            if names:
                return self.tool_remove_codes([c for c, _ in names])
        if wants_add and (wants_watch or self._extract_stocks(t)):
            names = self._extract_stocks(t)
            if names:
                return self.tool_add_codes(dict(names))
            return ChatResult("어떤 종목을 추가할까요? 종목명 또는 6자리 코드를 말씀해 주세요. 예: \"삼성전자, 카카오 추가\" / \"코스닥 상위 30개 추가\"", suggestions=["코스닥 거래량 상위 30개 추가", "코스피 시가총액 상위 20개 추가", "삼성전자 SK하이닉스 추가"])
        if any(k in low for k in ("추천", "매수신호", "매수 신호", "살만", "살 만", "뭐사", "뭐 사")) and not wants_add:
            return self.tool_recommendations()
        if wants_watch and any(k in low for k in ("보여", "목록", "뭐", "알려", "확인", "리스트", "몇")):
            return self.tool_list_watchlist()
        # --- 설정 변경 (숫자/시각이 함께 오면 엔진 제어보다 우선) ---
        setting = self._parse_setting(t)
        if setting and re.search(r"\d", t):
            return self.tool_set_setting(*setting)
        # --- 엔진 제어 ---
        if any(k in low for k in ("정지", "중지", "멈춰", "멈추", "꺼줘", "꺼주", "종료")) and any(k in low for k in ("엔진", "프로그램", "매매", "실행", "정지", "중지", "멈")) and "자동매매" not in low:
            return self.tool_stop()
        if "자동매매" in low or "자동 매매" in t:
            if any(k in low for k in ("꺼", "off", "중지", "끄", "해제")):
                return self.tool_auto(False)
            if any(k in low for k in ("켜", "on", "시작", "활성")):
                return self.tool_auto(True)
        if any(k in low for k in ("전량", "전부", "모두", "다")) and any(k in low for k in ("청산", "매도", "팔아")):
            return self.tool_close_all(confirm)
        if any(k in low for k in ("시작", "실행", "켜", "돌려", "가동")) and not any(k in low for k in ("백테스트", "사용법", "어떻게")):
            if self.ctl.engine_running:
                return ChatResult("엔진이 이미 실행 중입니다. 먼저 \"정지해줘\"라고 하세요.", suggestions=["엔진 정지", "상태 알려줘"])
            signal_only = any(k in low for k in ("시그널만", "신호만", "알림만", "주문없이", "주문 없이"))
            if any(k in low for k in ("시뮬", "체험", "데모", "sim")):
                return self.tool_start("sim", "paper", signal_only)
            if any(k in low for k in ("kis", "한투", "한국투자", "증권사", "모의투자", "실계좌")):
                return self.tool_start("kis", "live", signal_only, confirm)
            if any(k in low for k in ("네이버", "실시간", "페이퍼", "paper", "시그널")):
                return self.tool_start("naver", "paper", signal_only)
            return ChatResult("어떤 방식으로 시작할까요?", suggestions=["시뮬레이션 시작", "실시간 시그널만 시작", "페이퍼 트레이딩 시작", "KIS 모의투자 시작"])
        # --- 조회 ---
        if "백테스트" in low:
            m = re.search(r"(\d+)\s*일", t)
            days = int(m.group(1)) if m else 10
            feed = "naver" if "네이버" in low or "실제" in low else ("kis" if "kis" in low else "sim")
            if any(k in low for k in ("결과", "어때", "보여")) and not any(k in low for k in ("돌려", "실행", "해줘", "시작")):
                return self.tool_backtest_result()
            return self.tool_backtest(days, feed)
        if any(k in low for k in ("추천", "매수신호", "매수 신호", "살만", "살 만", "뭐사", "뭐 사")):
            return self.tool_recommendations()
        if any(k in low for k in ("보유", "포지션", "들고")):
            return self.tool_positions()
        if any(k in low for k in ("수익률", "손익", "성과", "얼마벌", "얼마 벌", "수익")) and "사용법" not in low:
            m = re.search(r"(\d+)\s*일", t)
            days = int(m.group(1)) if m else (1 if "오늘" in low else 7 if "주" in low else 30 if "달" in low or "월" in low else 30)
            return self.tool_performance(days)
        if any(k in low for k in ("상태", "현황", "요약", "어떻게돼", "어때")) and not any(k in low for k in ("수익", "추천")):
            return self.tool_status()
        if "뉴스" in low or "거시" in low or "시장분위기" in low or "시장 분위기" in t:
            return self.tool_news(fetch=any(k in low for k in ("수집", "가져", "갱신", "새로")))
        if any(k in low for k in ("연결", "진단", "접속", "인터넷")) and any(k in low for k in ("확인", "진단", "테스트", "돼", "되", "점검")):
            return self.tool_diagnose()
        # --- 설정 변경 ---
        setting = self._parse_setting(t)
        if setting:
            return self.tool_set_setting(*setting)
        # --- 도움말 ---
        return self.tool_help(t)

    # ======================= 파싱 유틸 =======================
    def _extract_stocks(self, t: str) -> list[tuple[str, str]]:
        found: dict[str, str] = {}
        for code in re.findall(r"(?<!\d)(\d{6})(?!\d)", t):
            found[code] = (self.ctl.cfg.get("watchlist") or {}).get(code, "") or next((n for n, c in KNOWN_STOCKS.items() if c == code), "")
        clean = t
        for name, code in sorted(KNOWN_STOCKS.items(), key=lambda x: -len(x[0])):
            if name.lower() in clean.lower():
                found.setdefault(code, name)
                clean = re.sub(re.escape(name), " ", clean, flags=re.I)
        for code, name in (self.ctl.cfg.get("watchlist") or {}).items():
            if name and name in t:
                found.setdefault(code, name)
        return list(found.items())

    def _parse_setting(self, t: str):
        low = t.lower()
        for keys, path, label in SETTING_ALIASES:
            hit = next((k for k in keys if k in low), None)
            if hit:
                m = re.search(r"(\d{1,2}:\d{2})", t)
                if m and path.startswith("risk.") and path.split(".")[1] in ("entry_start", "entry_end", "force_close"):
                    return path, m.group(1), label
                rest = re.sub(re.escape(hit), " ", low)  # "1회 손실" 의 1 처럼 별칭 안의 숫자는 제외
                m = re.search(r"(-?\d+(?:\.\d+)?)\s*(%|퍼센트|r|배|개|분|초|원|점)?", rest)
                if not m:
                    return None
                v = float(m.group(1))
                unit = (m.group(2) or "").lower()
                if path in ("risk.risk_per_trade", "risk.max_position_pct", "risk.daily_loss_limit_pct") and (unit in ("%", "퍼센트") or v >= 1):
                    v = v / 100.0
                if path in ("risk.max_positions", "interval_min", "initial_cash"):
                    v = int(v)
                return path, v, label
        return None

    # ======================= 도구 (규칙·LLM 공용) =======================
    def tool_add_top(self, market: str, n: int, by: str = "volume") -> ChatResult:
        from .screener import top_codes

        n = max(1, min(int(n), 100))
        exclude = set(self.ctl.cfg.get("watchlist") or {})
        try:
            kis = None
            if (self.ctl.cfg.get("kis") or {}).get("app_key"):
                from .factory import make_kis_client

                try:
                    kis = make_kis_client(self.ctl.cfg)
                except Exception:
                    kis = None
            found = top_codes(market, n, by, exclude, kis)
        except Exception as e:
            return ChatResult(f"상위 종목 조회에 실패했습니다: {e}\n인터넷 연결을 확인하거나 홈의 연결 진단을 실행해 보세요.")
        if not found:
            return ChatResult("조회된 종목이 없습니다 (이미 모두 관심종목에 있거나 네트워크 문제). 홈의 연결 진단을 확인해 주세요.")
        res = self.ctl.watchlist_update(add=found)
        names = ", ".join(f"{nm}({c})" for c, nm in list(found.items())[:15]) + (" …" if len(found) > 15 else "")
        mk = {"KOSDAQ": "코스닥", "KOSPI": "코스피"}.get(market.upper(), "코스피+코스닥")
        byk = "시가총액" if by == "marketcap" else "거래량"
        live = " 실행 중인 엔진에도 즉시 구독했습니다." if self.ctl.engine_running else ""
        return ChatResult(f"{mk} {byk} 상위 {len(found)}개를 관심종목에 추가했습니다 (총 {res['total']}종목).{live}\n{names}", actions=[f"watchlist_add:{len(found)}"], suggestions=["관심종목 보여줘", "시뮬레이션 시작", "지금 추천 종목 알려줘"], data={"added": found})

    def tool_add_codes(self, codes: dict[str, str]) -> ChatResult:
        clean = {}
        for c, n in codes.items():
            c = str(c).strip()
            if not c.isdigit() or len(c) > 6:
                # 이름만 준 경우: 알려진 종목 사전 → 네이버 검색
                code = KNOWN_STOCKS.get(c) or KNOWN_STOCKS.get(c.upper())
                if not code:
                    try:
                        from .data.naver import search_stock

                        hits = search_stock(c)
                        if hits:
                            code, n = hits[0]
                    except Exception:
                        code = None
                if not code:
                    return ChatResult(f"'{c}' 종목을 찾지 못했습니다. 6자리 종목코드로 말씀해 주세요 (예: 005930 추가).")
                c = code
            clean[c.zfill(6)] = n or ""
        if not clean:
            return ChatResult("추가할 종목을 찾지 못했습니다.")
        # 이름이 비어 있으면 조회 시도
        for c in list(clean):
            if not clean[c]:
                try:
                    clean[c] = self.ctl.lookup(c)["name"]
                except Exception:
                    clean[c] = next((n for n, cc in KNOWN_STOCKS.items() if cc == c), "")
        res = self.ctl.watchlist_update(add=clean)
        added = res["added"]
        already = [c for c in clean if c not in added]
        msg = ""
        if added:
            msg += "추가: " + ", ".join(f"{clean[c] or c}({c})" for c in added)
        if already:
            msg += ("\n" if msg else "") + "이미 있음: " + ", ".join(f"{clean[c] or c}({c})" for c in already)
        return ChatResult(msg + f"\n현재 관심종목 {res['total']}개." + (" 실행 중인 엔진에도 구독했습니다." if added and self.ctl.engine_running else ""), actions=[f"watchlist_add:{len(added)}"], suggestions=["관심종목 보여줘", "지금 추천 종목 알려줘"])

    def tool_remove_codes(self, codes: list[str]) -> ChatResult:
        res = self.ctl.watchlist_update(remove=codes)
        msg = ""
        if res["removed"]:
            msg += "삭제: " + ", ".join(res["removed"])
        if res["kept_positions"]:
            msg += ("\n" if msg else "") + "보유 중이라 삭제하지 않음: " + ", ".join(res["kept_positions"])
        if not msg:
            msg = "관심종목에 없는 종목입니다."
        return ChatResult(msg + f"\n현재 관심종목 {res['total']}개.", actions=[f"watchlist_remove:{len(res['removed'])}"])

    def tool_clear_watchlist(self, confirm: bool) -> ChatResult:
        wl = self.ctl.cfg.get("watchlist") or {}
        if not confirm:
            return ChatResult(f"관심종목 {len(wl)}개를 모두 삭제할까요? (보유 중인 종목은 남습니다)", needs_confirm={"message": "관심종목 전체 삭제", "payload": "관심종목 전부 삭제"})
        return self.tool_remove_codes(list(wl))

    def tool_list_watchlist(self) -> ChatResult:
        wl = self.ctl.cfg.get("watchlist") or {}
        if not wl:
            return ChatResult("관심종목이 비어 있습니다. \"코스닥 거래량 상위 20개 추가\"처럼 말씀해 보세요.")
        lines = [f"- {n or '(이름 없음)'} {c}" for c, n in wl.items()]
        return ChatResult(f"관심종목 {len(wl)}개:\n" + "\n".join(lines[:60]) + ("\n…" if len(lines) > 60 else ""))

    def tool_start(self, feed: str, mode: str, signal_only: bool = False, confirm: bool = False) -> ChatResult:
        if self.ctl.engine_running:
            return ChatResult("엔진이 이미 실행 중입니다. 먼저 \"정지해줘\"라고 하세요.", suggestions=["엔진 정지", "상태 알려줘"])
        if feed == "kis" and not self.ctl.status()["kis_configured"]:
            return ChatResult("한국투자증권 앱키·시크릿·계좌가 설정되지 않았습니다. 설정 → 한국투자증권 API 에 입력 후 저장하세요.", suggestions=["KIS 연동 방법 알려줘"])
        if feed == "kis" and mode == "live" and not bool((self.ctl.cfg.get("kis") or {}).get("paper", True)) and not confirm:
            return ChatResult("실계좌로 실제 주문이 전송됩니다. 정말 시작할까요?", needs_confirm={"message": "실계좌 주문 시작", "payload": "KIS 실계좌 시작 확인"})
        try:
            run = self.ctl.start(feed=feed, mode=mode, signal_only=signal_only, use_news=(feed != "sim"), sim_speed=None)
        except Exception as e:
            return ChatResult(f"시작 실패: {e}")
        label = {"sim": "시뮬레이션", "naver": "네이버 실시간", "kis": "한국투자증권"}[feed]
        return ChatResult(f"{label} 엔진을 시작했습니다 ({'시그널만' if signal_only else '자동매매'} · {len(run['codes'])}종목). 대시보드에서 확인하세요.", actions=["engine_start"], suggestions=["상태 알려줘", "지금 추천 종목 알려줘", "엔진 정지"], data={"navigate": "dash"})

    def tool_stop(self) -> ChatResult:
        if not self.ctl.engine_running:
            return ChatResult("엔진이 실행 중이 아닙니다.")
        ok = self.ctl.stop()
        return ChatResult("엔진을 정지했습니다." if ok else "정지 요청을 보냈지만 아직 종료 중입니다. 잠시 후 상태를 확인하세요.", actions=["engine_stop"])

    def tool_auto(self, on: bool) -> ChatResult:
        if not self.ctl.engine_running:
            return ChatResult("엔진이 실행 중이 아닙니다. 시작한 뒤 자동매매를 전환할 수 있습니다.")
        cur = self.ctl.engine.trader.auto_trade
        if cur != on:
            self.ctl.toggle_auto()
        return ChatResult(f"자동매매를 {'켰습니다' if on else '껐습니다'}. " + ("이제 신호에 따라 주문합니다." if on else "이제 신호만 알리고 주문하지 않습니다."), actions=[f"auto:{on}"])

    def tool_close_all(self, confirm: bool) -> ChatResult:
        if not self.ctl.engine_running:
            return ChatResult("엔진이 실행 중이 아닙니다.")
        n = len(self.ctl.engine.broker.positions())
        if n == 0:
            return ChatResult("보유 중인 포지션이 없습니다.")
        if not confirm:
            return ChatResult(f"보유 포지션 {n}개를 현재가로 전량 청산할까요?", needs_confirm={"message": "전량 청산", "payload": "전량 청산 확인"})
        closed = self.ctl.close_all()
        return ChatResult(f"{closed}개 포지션 청산 요청을 보냈습니다.", actions=["close_all"])

    def tool_status(self) -> ChatResult:
        st = self.ctl.status()
        lines = [f"- 시장: {'정규장 진행 중' if st['market_open'] else '장 마감'}"]
        if st["running"]:
            s = self.ctl.state()
            ro = st["run_opts"]
            lines.append(f"- 엔진: 실행 중 ({ro['feed']}/{ro['mode']}{' · 시그널만' if ro.get('signal_only') else ''}) · {s.get('status', '')}")
            lines.append(f"- 총자산 {s.get('equity', 0):,.0f}원 · 실현 {_won(s.get('realized', 0))} · 평가 {_won(s.get('unrealized', 0))} · 일간 {s.get('day_pct', 0):+.2f}%")
            lines.append(f"- 거래 {s.get('trades', 0)}건 (승 {s.get('wins', 0)}/패 {s.get('losses', 0)}) · 보유 {len(s.get('positions', []))}종목 · 자동매매 {'ON' if s.get('auto_trade') else 'OFF'}")
            if s.get("warming"):
                lines.append(f"- 캔들 누적 중: {len(s['warming'])}종목 (시그널까지 시간이 걸립니다)")
            if s.get("halted"):
                lines.append(f"- ⚠ {s['halted']}")
        else:
            lines.append("- 엔진: 대기 중" + (f" (마지막 오류: {st['last_error']})" if st.get("last_error") else ""))
        lines.append(f"- 관심종목 {st['watchlist_count']}개 · KIS {'설정됨' if st['kis_configured'] else '미설정'}")
        return ChatResult("현재 상태:\n" + "\n".join(lines), suggestions=["지금 추천 종목 알려줘", "오늘 수익률 어때?", "시뮬레이션 시작"])

    def tool_recommendations(self) -> ChatResult:
        if not self.ctl.engine_running:
            return ChatResult("엔진이 실행 중이 아니라 실시간 추천을 계산할 수 없습니다. \"시뮬레이션 시작\" 또는 \"실시간 시그널만 시작\"이라고 말씀해 보세요.", suggestions=["시뮬레이션 시작", "실시간 시그널만 시작"])
        s = self.ctl.state()
        rc = s.get("recommendations") or {}
        out = []
        for r in (rc.get("buy") or [])[:5]:
            sg = r["suggest"]
            out.append(f"▲ {r['name']}({r['code']}) 점수 {r['score']:+.0f} · {r['price']:,.0f}원 · 제안 {sg['qty']}주, 손절 {sg['stop']:,.0f}, 목표 {sg['take_profit']:,.0f}" + (f" · 보류: {r['blocked']}" if r.get("blocked") else "") + f"\n   근거: {', '.join(r['reasons'][:3])}")
        for r in (rc.get("watch") or [])[:3]:
            out.append(f"◆ 관심 {r['name']}({r['code']}) 점수 {r['score']:+.0f} · {', '.join(r['reasons'][:2])}")
        for r in (rc.get("sell") or [])[:3]:
            out.append(f"▼ {r['label']} {r['name']}({r['code']}) 점수 {r['score']:+.0f} · {', '.join(r['reasons'][:2])}")
        if not out:
            ranked = sorted(s.get("watchlist") or [], key=lambda w: -(w.get("score") or 0))[:5]
            top = ", ".join(f"{w['name']} {w.get('score', 0):+.0f}" for w in ranked)
            return ChatResult(f"현재 매수·매도 임계값을 넘은 종목이 없습니다 (유니버스 {rc.get('universe', 0)}종목). 점수 상위: {top or '계산 중'}", suggestions=["코스닥 거래량 상위 30개 추가", "매수 임계값 45로 낮춰줘"], data={"navigate": "reco"})
        return ChatResult("실시간 추천:\n" + "\n".join(out) + "\n\n자세한 카드는 추천 종목 페이지에 있습니다. 투자 판단과 손실은 사용자 책임입니다.", data={"navigate": "reco"})

    def tool_positions(self) -> ChatResult:
        if not self.ctl.engine_running:
            return ChatResult("엔진이 실행 중이 아닙니다.")
        s = self.ctl.state()
        pos = s.get("positions") or []
        if not pos:
            return ChatResult("보유 중인 포지션이 없습니다.")
        lines = [f"- {p['name']} {p['qty']}주 평단 {p['avg_price']:,.0f} → 현재 {p['price']:,.0f} ({p['pnl_pct']:+.2f}%, {_won(p['pnl'])}) 손절 {p['stop']:,.0f} 익절 {p['take_profit']:,.0f}" for p in pos]
        return ChatResult(f"보유 {len(pos)}종목:\n" + "\n".join(lines), suggestions=["전량 청산", "상태 알려줘"])

    def tool_performance(self, days: int = 30) -> ChatResult:
        rep = self.ctl.performance(days or None)
        k = rep["kpi"]
        if k["trades"] == 0:
            return ChatResult(f"최근 {days}일 동안 기록된 실거래·페이퍼 거래가 없습니다. (시뮬레이션 거래는 수익률 페이지에서 '시뮬레이션 포함'으로 볼 수 있습니다)", data={"navigate": "perf"})
        pf = k["profit_factor"] if k["profit_factor"] is not None else "-"
        msg = (f"최근 {days}일 성과: 누적 손익 {_won(k['total_pnl'])} ({k['return_pct']:+.2f}%), 거래 {k['trades']}건, 승률 {k['win_rate']}%, 손익비 {pf}, "
               f"기대손익/거래 {_won(k['expectancy'])}, 최대낙폭 {_won(k['max_drawdown'])}, 최대 연승/연패 {k['best_streak']}/{k['worst_streak']}, 평균 보유 {k['avg_holding_min']}분")
        best = rep["per_code"][:3]
        if best:
            msg += "\n종목별 상위: " + ", ".join(f"{c['name']} {_won(c['pnl'])}" for c in best)
        return ChatResult(msg, data={"navigate": "perf"})

    def tool_backtest(self, days: int = 10, feed: str = "sim") -> ChatResult:
        try:
            self.ctl.backtest_start(days=days, feed=feed)
        except Exception as e:
            return ChatResult(f"백테스트 시작 실패: {e}")
        return ChatResult(f"{'시뮬레이션' if feed == 'sim' else feed} 데이터로 {days}일 백테스트를 시작했습니다. 백테스트 페이지에서 진행 상황과 결과를 볼 수 있습니다. 끝나면 \"백테스트 결과 알려줘\"라고 물어보세요.", actions=["backtest_start"], suggestions=["백테스트 결과 알려줘"], data={"navigate": "backtest"})

    def tool_backtest_result(self) -> ChatResult:
        j = self.ctl.backtest.to_dict()
        if j["status"] == "running":
            return ChatResult(f"백테스트 실행 중입니다… {j['progress']} ({j['elapsed']}s)", suggestions=["백테스트 결과 알려줘"])
        if j["status"] != "done" or not j["result"]:
            return ChatResult("완료된 백테스트가 없습니다. \"10일 백테스트 돌려줘\"라고 말씀해 보세요." + (f" (오류: {j['error']})" if j.get("error") else ""))
        s = j["result"]["summary"]
        keys = ["총수익률(%)", "거래횟수", "승률(%)", "손익비(PF)", "최대낙폭(%)", "샤프(일간)", "평균보유(분)"]
        return ChatResult("백테스트 결과: " + " · ".join(f"{k} {s[k]}" for k in keys if k in s), data={"navigate": "backtest"})

    def tool_set_setting(self, path: str, value, label: str = "") -> ChatResult:
        try:
            self.ctl.set_settings({path: value})
        except Exception as e:
            return ChatResult(f"설정 변경 실패: {e}")
        shown = f"{value * 100:.2f}%" if path in ("risk.risk_per_trade", "risk.max_position_pct", "risk.daily_loss_limit_pct") else value
        note = " 실행 중인 엔진에는 다음 시작부터 적용됩니다." if self.ctl.engine_running else ""
        return ChatResult(f"{label or path} 을(를) {shown} 로 저장했습니다.{note}", actions=[f"set:{path}"])

    def tool_news(self, fetch: bool = False) -> ChatResult:
        d = self.ctl.news(fetch=fetch)
        lines = []
        for i in d.get("indices") or []:
            lines.append(f"- {i['name']} {i['value']:,.2f} ({i['change_pct']:+.2f}%)")
        lines.append(f"- 시장 편향 {d.get('market_bias', 0):+.2f} · 뉴스 {d.get('news_count', 0)}건" + (f" · 오류: {'; '.join(d['errors'])}" if d.get("errors") else ""))
        for s in (d.get("sectors") or [])[:5]:
            lines.append(f"- 섹터 {s['sector']}: RS {s['rs']:+.2f}%p, 감성 {s['sentiment']:+.2f} ({s['count']}건)")
        for n in (d.get("news") or [])[:5]:
            lines.append(f"- [{n['sentiment']:+.1f}] {n['title'][:60]}")
        if not d.get("news") and not fetch:
            lines.append("아직 수집된 뉴스가 없습니다. \"뉴스 수집해줘\"라고 하면 지금 가져옵니다.")
        return ChatResult("뉴스·거시:\n" + "\n".join(lines), suggestions=["뉴스 수집해줘", "지금 추천 종목 알려줘"], data={"navigate": "news"})

    def tool_diagnose(self) -> ChatResult:
        d = self.ctl.diagnose()
        lines = [f"- {r['label']}: {'연결됨' if r['ok'] else ('미설정' if r['ok'] is None else '실패')} ({r['ms']}ms) {r['detail'][:60]}" for r in d["results"]]
        return ChatResult("연결 진단:\n" + "\n".join(lines) + (f"\n\n{d['hint']}" if d.get("hint") else ""))

    def tool_help(self, question: str = "") -> ChatResult:
        low = question.lower().replace(" ", "")
        scored = []
        for tpc in HELP_TOPICS:
            score = sum(1 for k in tpc["keys"] if k.replace(" ", "").lower() in low)
            if score:
                scored.append((score, tpc))
        scored.sort(key=lambda x: -x[0])
        if scored:
            best = [t for _, t in scored[:2]]
            return ChatResult("\n\n".join(f"**{t['title']}**\n{t['answer']}" for t in best), suggestions=["사용법 알려줘", "시그널 점수는 뭐야?", "KIS 연동 방법"])
        return ChatResult("이렇게 말씀해 보세요:\n- \"코스닥 거래량 상위 20개 관심종목에 추가\" / \"삼성전자 추가\" / \"카카오 빼줘\" / \"관심종목 보여줘\"\n- \"시뮬레이션 시작\" / \"실시간 시그널만 시작\" / \"엔진 정지\" / \"자동매매 꺼줘\" / \"전량 청산\"\n- \"지금 추천 종목\" / \"보유 종목\" / \"오늘 수익률\" / \"상태\"\n- \"10일 백테스트 돌려줘\" / \"백테스트 결과\"\n- \"손절 ATR 배수 2로\" / \"매수 임계값 60\" / \"1회 손실 0.5%\"\n- \"뉴스 수집해줘\" / \"연결 진단해줘\"\n- 사용법 질문: \"시그널 점수는 뭐야?\", \"KIS 연동 방법\", \"장 시간\"", suggestions=STARTER_SUGGESTIONS)

    # ======================= Claude 연동 =======================
    TOOLS = [
        {"name": "add_top_stocks", "description": "거래량 또는 시가총액 상위 N개 종목을 관심종목에 추가한다.", "input_schema": {"type": "object", "properties": {"market": {"type": "string", "enum": ["KOSPI", "KOSDAQ", "ALL"]}, "n": {"type": "integer", "minimum": 1, "maximum": 100}, "by": {"type": "string", "enum": ["volume", "marketcap"]}}, "required": ["market", "n"]}},
        {"name": "add_stocks", "description": "종목명 또는 6자리 코드 목록을 관심종목에 추가한다.", "input_schema": {"type": "object", "properties": {"stocks": {"type": "array", "items": {"type": "string"}}}, "required": ["stocks"]}},
        {"name": "remove_stocks", "description": "종목 코드 목록을 관심종목에서 제거한다 (보유 중 종목은 유지).", "input_schema": {"type": "object", "properties": {"codes": {"type": "array", "items": {"type": "string"}}}, "required": ["codes"]}},
        {"name": "list_watchlist", "description": "현재 관심종목 목록.", "input_schema": {"type": "object", "properties": {}}},
        {"name": "start_engine", "description": "매매 엔진 시작. feed: sim(시뮬레이션)|naver(실시간)|kis(한국투자증권). signal_only 면 주문 없이 신호만.", "input_schema": {"type": "object", "properties": {"feed": {"type": "string", "enum": ["sim", "naver", "kis"]}, "signal_only": {"type": "boolean"}}, "required": ["feed"]}},
        {"name": "stop_engine", "description": "엔진 정지.", "input_schema": {"type": "object", "properties": {}}},
        {"name": "set_auto_trade", "description": "자동매매 켜기/끄기.", "input_schema": {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}},
        {"name": "close_all_positions", "description": "보유 포지션 전량 청산. 사용자가 명확히 확인한 경우에만 confirm=true.", "input_schema": {"type": "object", "properties": {"confirm": {"type": "boolean"}}, "required": ["confirm"]}},
        {"name": "get_status", "description": "엔진/계좌/시장 상태 요약.", "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_recommendations", "description": "실시간 매수/관심/매도 추천.", "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_positions", "description": "보유 포지션.", "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_performance", "description": "최근 N일 수익률/성과 통계.", "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}, "required": ["days"]}},
        {"name": "run_backtest", "description": "백테스트 실행 (백그라운드).", "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}, "feed": {"type": "string", "enum": ["sim", "naver", "kis"]}}, "required": ["days"]}},
        {"name": "get_backtest_result", "description": "마지막 백테스트 결과.", "input_schema": {"type": "object", "properties": {}}},
        {"name": "set_setting", "description": "설정 변경. path 예: strategy.buy_threshold, risk.atr_stop_mult, risk.risk_per_trade(비율 0.01=1%), risk.max_positions, risk.entry_start('09:10'), initial_cash, interval_min", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "value": {}}, "required": ["path", "value"]}},
        {"name": "get_news", "description": "뉴스·거시 컨텍스트 요약. fetch=true 면 지금 수집.", "input_schema": {"type": "object", "properties": {"fetch": {"type": "boolean"}}}},
        {"name": "diagnose_connections", "description": "인터넷 데이터 소스 연결 진단.", "input_schema": {"type": "object", "properties": {}}},
        {"name": "help", "description": "프로그램 사용법 문서에서 주제 검색.", "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    ]

    def _run_tool(self, name: str, inp: dict, confirm: bool) -> ChatResult:
        if name == "add_top_stocks":
            return self.tool_add_top(inp.get("market", "ALL"), int(inp.get("n", 20)), inp.get("by", "volume"))
        if name == "add_stocks":
            return self.tool_add_codes({s: "" for s in inp.get("stocks", [])})
        if name == "remove_stocks":
            return self.tool_remove_codes([str(c) for c in inp.get("codes", [])])
        if name == "list_watchlist":
            return self.tool_list_watchlist()
        if name == "start_engine":
            feed = inp.get("feed", "sim")
            return self.tool_start(feed, "live" if feed == "kis" else "paper", bool(inp.get("signal_only")), confirm)
        if name == "stop_engine":
            return self.tool_stop()
        if name == "set_auto_trade":
            return self.tool_auto(bool(inp.get("on")))
        if name == "close_all_positions":
            return self.tool_close_all(bool(inp.get("confirm")) and confirm)
        if name == "get_status":
            return self.tool_status()
        if name == "get_recommendations":
            return self.tool_recommendations()
        if name == "get_positions":
            return self.tool_positions()
        if name == "get_performance":
            return self.tool_performance(int(inp.get("days", 30)))
        if name == "run_backtest":
            return self.tool_backtest(int(inp.get("days", 10)), inp.get("feed", "sim"))
        if name == "get_backtest_result":
            return self.tool_backtest_result()
        if name == "set_setting":
            return self.tool_set_setting(str(inp.get("path")), inp.get("value"))
        if name == "get_news":
            return self.tool_news(bool(inp.get("fetch")))
        if name == "diagnose_connections":
            return self.tool_diagnose()
        if name == "help":
            return self.tool_help(str(inp.get("question", "")))
        return ChatResult(f"알 수 없는 도구: {name}")

    def _system_prompt(self) -> str:
        guide = "\n\n".join(f"## {t['title']}\n{t['answer']}" for t in HELP_TOPICS)
        return ("당신은 한국주식 데이트레이딩 프로그램 K-DayTrader 의 도우미입니다. 한국어로 간결하게 답하고, 사용자의 요청은 제공된 도구로 실행하세요. "
                "돈이 걸린 행동(실계좌 시작, 전량 청산)은 사용자가 명시적으로 확인하지 않았다면 confirm=false 로 호출해 확인 절차를 따르세요. "
                "투자 조언을 할 때는 프로그램의 시그널이 수익을 보장하지 않는다는 점을 짧게 덧붙이세요.\n\n# 사용법 문서\n" + guide)

    def _handle_llm(self, text: str, history: list[dict], cfg: dict) -> ChatResult | None:
        try:
            import anthropic
        except ImportError:
            return None  # SDK 미설치 → 규칙 기반
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=60.0, max_retries=1)
        model = cfg.get("model") or "claude-opus-5"
        messages = [{"role": m["role"], "content": str(m.get("content", ""))[:2000]} for m in history[-10:] if m.get("role") in ("user", "assistant") and m.get("content")]
        messages.append({"role": "user", "content": text})
        actions: list[str] = []
        confirm_req = None
        navigate = None
        params = dict(model=model, max_tokens=4000, system=self._system_prompt(), tools=self.TOOLS, messages=messages, output_config={"effort": cfg.get("effort") or "low"})
        for _ in range(6):
            try:
                response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
            except TypeError:  # 구버전 SDK: 폴백 파라미터 미지원
                response = client.messages.create(**params)
            if response.stop_reason == "refusal":
                return ChatResult("요청을 처리할 수 없습니다 (안전 정책). 다른 표현으로 말씀해 주세요.", source="claude")
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for tu in tool_uses:
                r = self._run_tool(tu.name, dict(tu.input or {}), False)
                actions += r.actions
                confirm_req = confirm_req or r.needs_confirm
                navigate = (r.data or {}).get("navigate") or navigate
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": r.reply[:4000]})
            messages.append({"role": "user", "content": results})
        text_out = "\n".join(b.text for b in response.content if b.type == "text").strip() or "처리했습니다."
        return ChatResult(text_out, actions, [], confirm_req, {"navigate": navigate} if navigate else None, source="claude")
