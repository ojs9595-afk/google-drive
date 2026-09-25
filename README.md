# kdaytrader — 한국주식 실시간 기술적 분석 데이트레이딩 엔진

한국거래소(코스피/코스닥) 종목의 실시간 시세를 받아 다중 지표 앙상블 + 퀀트 알파 + 거시/섹터/뉴스 컨텍스트로
매수·매도 시그널을 만들고, 리스크 규칙에 따라 자동(또는 알림만) 매매하는 데이트레이딩 프로그램입니다.

> **면책**: 이 프로그램은 어떤 수익률도 보장하지 않습니다. 모든 매매 손실은 사용자 책임입니다.
> 실계좌(`mode: live`)로 돌리기 전에 반드시 백테스트와 모의투자(`kis.paper: true`)로 충분히 검증하세요.

## 주요 기능

| 영역 | 내용 |
|---|---|
| 실시간 피드 | 한국투자증권 Open API 웹소켓(체결·호가잔량·체결강도), 네이버 금융 폴링(키 불필요), 시뮬레이션 |
| 기술적 분석 | EMA 9/21/50, VWAP, RSI, MACD, 볼린저(스퀴즈), ATR, SuperTrend, ADX, 스토캐스틱, 시가범위(ORB) 돌파, 거래량 비율 |
| 국면 적응 | ADX 로 추세장/횡보장 판정 → 추세추종·돌파 vs 평균회귀 가중치 자동 전환 |
| 퀀트 알파 | 페어 스프레드 z-score(통계적 차익거래), 마이크로스트럭처(호가 불균형·체결흐름·체결강도·마이크로프라이스), 횡단면 모멘텀, 시계열 모멘텀, WorldQuant식 공식형 알파, 변동성 국면 |
| 거시·섹터·뉴스 | 코스피/코스닥 지수 흐름(급락 시 진입 차단·긴급 청산), RSS 실시간 뉴스 감성(거시/산업/종목), 섹터 상대강도, 예정 이벤트 회피 |
| 리스크 관리 | ATR 손절, R배수 익절, 부분 익절, 본전 이동·트레일링 스탑, 종목당 비중·최대 종목 수, 일일 손실 한도, 손절 후 쿨다운, 진입 시간대, 동시호가 전 강제 청산, 변동성 타게팅, 분수 켈리 사이징 |
| 실행 | 페이퍼 브로커(슬리피지·수수료·거래세 반영), 한국투자증권 실전/모의 주문 |
| 도구 | Rich 터미널 대시보드, 텔레그램 알림, 분봉 백테스터(수익률·승률·PF·MDD·샤프), 파라미터 그리드 탐색, 거래량 상위 스크리너 |

## 다운로드 / 실행

- GitHub 브랜치 ZIP: https://github.com/ojs9595-afk/google-drive/archive/refs/heads/claude/korean-stock-day-trading-11txe6.zip
- 압축을 풀고 `start.bat`(Windows) 또는 `./start.sh`(macOS/Linux) 를 실행하면 가상환경 생성 → 의존성 설치 → 메뉴가 뜹니다.
- 인자 없이 `python main.py` 를 실행해도 같은 메뉴가 나옵니다. 1번(데모)을 고르면 브라우저 대시보드가 자동으로 열립니다.

## 웹 대시보드

`run`/`demo` 실행 시 http://127.0.0.1:8787 에서 브라우저 대시보드가 열립니다 (`--open-browser` 로 자동 열기, `--no-web` 로 비활성, `--port` 로 포트 변경).
총자산·손익·일간 수익률 KPI, 종목별 시그널 점수 바와 근거, 보유 포지션(손절/익절/R), 자산 곡선, 체결 로그,
코스피/코스닥·시장 편향, 섹터 보드, 실시간 뉴스 감성을 1초마다 갱신하며 자동매매 ON/OFF 와 전량 청산 버튼을 제공합니다.
라이트/다크 테마를 지원하고 추가 패키지가 필요 없습니다(표준 라이브러리 HTTP 서버).

## 설치

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 필요 시 수정
```

Python 3.10 이상.

## 빠른 시작

```bash
# 1) 시뮬레이션 데모 (네트워크 불필요, 1분봉을 120배속 재생)
python main.py demo

# 2) 백테스트 (시뮬레이션 데이터 10일)
python main.py backtest --days 10 --trades

# 3) 네이버 폴링으로 실시간 시그널만 보기 (주문 없음, 장중에만 의미 있음)
python main.py run --feed naver --signal-only

# 4) 한국투자증권 모의투자 자동매매
export KIS_APP_KEY=... KIS_APP_SECRET=... KIS_ACCOUNT=12345678-01
python main.py run --feed kis --mode live        # config 의 kis.paper: true 면 모의투자 서버

# 5) 현재 거시/섹터/종목 뉴스 컨텍스트 평가
python main.py news --stocks

# 6) 거래량 상위 스크리닝
python main.py screen
```

실계좌 주문(`mode: live` + `kis.paper: false`)은 실행 시 `yes` 확인을 요구합니다.

## 시그널 산출 방식

1. 틱을 1분(설정 가능) 캔들로 집계하고, 캔들이 닫힐 때마다 지표를 계산합니다.
2. 하위 신호(-1~+1)를 국면별 가중치로 합산해 -100~+100 점수로 정규화합니다.
   - 기술적: 추세(VWAP/EMA 정배열·크로스), 모멘텀(MACD·RSI·스토캐스틱), 돌파(ORB·볼린저 스퀴즈·20봉 신고가), 평균회귀(밴드 하단 과매도 반등), SuperTrend
   - 퀀트: 횡단면 모멘텀, 시계열 모멘텀, 페어 z-score, 마이크로스트럭처, 공식형 알파
   - 거래량 급증(2배 이상)은 점수를 15% 증폭, 거래량 부족은 30% 감쇄
3. 컨텍스트 보정: 지수 흐름·섹터 뉴스/상대강도·종목 뉴스 감성이 점수에 ±60 까지 가감되고,
   시장 급락·거시 악재·종목 악재·예정 이벤트 구간에서는 신규 진입을 차단합니다.
4. **매수**: 점수 ≥ `buy_threshold`(기본 55, 고변동성 국면 +5) 이고 VWAP 위 / RSI ≤ 78 / 거래량·변동성 필터 통과.
5. **청산**: 점수 ≤ `sell_threshold`, SuperTrend 하락 전환, VWAP 하회 + EMA 역배열, 종목 악재 속보, 또는 리스크 규칙(손절/익절/트레일링/시간).

## 퀀트 전략 출처와 구현 위치

`kdaytrader/strategy/quant.py` 는 주요 퀀트 하우스들이 **공개적으로 알려진** 전략 계열을 데이트레이딩 시간축에 맞게 단순화한 것입니다
(각 회사의 실제 비공개 모델이 아닙니다).

| 구성요소 | 전략 계열 (대표 회사) | 구현 |
|---|---|---|
| `pairs` | 통계적 차익거래·페어 (Renaissance, D.E. Shaw, PDT, Millennium, Two Sigma) | 롤링 OLS 헤지비율, 스프레드 z-score, OU 반감기·상관 필터 |
| `micro` | 마켓메이킹·마이크로스트럭처 (Jane Street, Citadel Securities, Optiver, IMC, SIG, Virtu, Flow Traders) | 호가잔량 불균형(OBI), 체결흐름 불균형(TFI), 체결강도, 마이크로프라이스 편차 EWMA |
| `xs_momentum` | 횡단면 모멘텀·팩터 (AQR, Two Sigma, Cubist, QRT, BAM) | 유니버스 대비 30봉 수익률 z-score |
| `tsmom` | 시계열 모멘텀·추세추종 (AQR TSMOM, DRW/Jump CTA 접근) | 20/60/120봉 ATR 정규화 수익률 |
| `alpha` | 공식형 알파 (WorldQuant 101 Alphas) | Alpha#101/#12/#9/#53 변형의 롤링 랭크 결합 |
| `vol_regime` | 변동성 타게팅·리스크 패리티 (AQR, Citadel/Millennium 리스크 그리드) | 단기/장기 실현변동성 비율로 사이즈 0.5~1.5배 조절, 고변동성 시 진입 임계값 상향 |
| `kelly_scale` | 켈리 자금관리 (Renaissance/Thorp 계보, PDT) | 최근 20거래 승률·손익비로 하프 켈리 → 리스크 예산 0.5~1.5배 |
| 국면 전환 | 레짐 스위칭 (Two Sigma, Renaissance 계열 HMM 접근의 단순화) | ADX 추세/횡보 + 변동성 국면 |

호가잔량·체결강도는 한국투자증권 웹소켓 피드에서만 제공되며, 네이버 피드에서는 `micro` 구성요소가 자동으로 제외됩니다.

## gs-quant 참고 분석 계층 (`kdaytrader/analytics.py`, `kdaytrader/strategy/rules.py`)

Goldman Sachs 의 오픈소스 [gs-quant](https://github.com/goldmansachs/gs-quant) (Apache-2.0) 에서 GS Marquee API 없이 쓸 수 있는
시계열 분석과 백테스트 설계를 순수 pandas/numpy 로 옮겼습니다.

| 이 프로젝트 | gs-quant 원본 | 쓰이는 곳 |
|---|---|---|
| `analytics.zscores / winsorize / percentiles` | `timeseries.statistics` | 평균회귀 트리거, 극단값 제한 |
| `analytics.exponential_std / exponential_volatility / volatility` | `statistics.exponential_std`, `technicals.exponential_volatility`, `econometrics.volatility` | 백테스트 지수가중 변동성, `analyze` 명령 |
| `analytics.beta / correlation` | `econometrics.beta / correlation` | 코스피 대비 롤링 베타 → 시장 편향을 종목 베타로 스케일(고베타 종목은 하락장에서 더 감점) |
| `analytics.max_drawdown / drawdown_duration / sharpe / sortino / calmar` | `econometrics.max_drawdown / sharpe_ratio` | 백테스트 성과 지표 확장 |
| `analytics.rolling_linear_regression` | `statistics.RollingLinearRegression` | 페어 헤지비율·R² (statsmodels 불필요) |
| `analytics.backtest_basket` | `timeseries.backtesting.backtest_basket` | 섹터/관심종목 바스켓 성과 비교 |
| `analytics.event_study` | `timeseries.event_study.event_impact_analysis` | 지수 급락 이벤트 전후 종목 반응 (`analyze` 명령) |
| `analytics.smooth_spikes / consecutive` | `timeseries.analysis` | 이상 틱 제거, 연속 상승 카운트 |
| `rules.MktTrigger / MeanReversionTrigger / RiskTrigger / TimeWindowTrigger / AggregateTrigger / NotTrigger` | `backtests.triggers` | 설정 파일로 정의하는 트리거 → 액션(점수 가감·진입 차단·청산) 규칙 엔진 |

`config.yaml` 의 `rules.triggers` 에 gs-quant 식 규칙을 선언할 수 있습니다:

```yaml
rules:
  enabled: true
  use_defaults: true        # z-score 평균회귀, RSI 85 진입 차단, -1.5R 청산, 개장 직후 감점
  triggers:
    - {kind: mkt, column: adx, level: 40, direction: above, actions: [{type: score, score: 8}], name: 강한 추세}
    - {kind: risk, measure: holding_min, level: 90, direction: above, actions: [{type: exit}], name: 90분 초과 청산}
    - {kind: time, start: "14:30", end: "15:00", actions: [{type: block_entry}], name: 마감 전 진입 금지}
```

```bash
python main.py analyze --feed naver --days 3     # 변동성·β·낙폭·z-score·지수급락 이벤트 반응·바스켓 성과
```

## 거시·섹터·뉴스 실시간 대응 (`kdaytrader/context.py`)

- **지수**: 코스피/코스닥을 5초마다 폴링. 등락률 ≤ -1.5% 또는 10분 모멘텀 ≤ -0.6% 이면 신규 진입 차단, ≤ -2.25% 면 보유분 긴급 청산.
- **뉴스**: 연합뉴스·한국경제·매일경제 RSS + 관심종목별 구글뉴스를 60초마다 수집. 한국어 금융 감성 사전으로 -1~+1 점수화, 90분 반감기로 감쇠.
  - 거시 키워드(금리·환율·관세·FOMC 등) 뉴스 → 전 종목 점수 보정, 강한 악재면 진입 차단
  - 섹터 키워드(반도체·2차전지·자동차·바이오 등 19개 섹터) → 해당 섹터 종목 점수 보정
  - 종목명 포함 뉴스 → 종목 점수 보정, 강한 악재(≤ -0.45)면 진입 차단, 20분 내 속보(≤ -0.7)면 즉시 청산
- **섹터 상대강도**: 관찰 종목의 등락률로 섹터별 RS 를 계산해 주도 섹터 종목을 우대.
- **예정 이벤트**: 설정한 시각 전후(예: 금통위, 미 CPI 여파) 신규 진입 회피.

`python main.py news` 로 현재 컨텍스트 평가를 확인할 수 있습니다.

## 백테스트

```bash
python main.py backtest --days 10 --trades                      # 시뮬레이션 데이터
python main.py backtest --feed naver --days 3 --codes 005930     # 네이버 분봉 (최근 수일치만 제공)
python main.py backtest --csv-dir ./data_cache                   # ts,open,high,low,close,volume CSV
python main.py backtest --grid '{"buy_threshold":[50,55,60],"atr_stop_mult":[1.2,1.5,2.0]}'
python main.py backtest --out backtest_results                   # equity_curve.csv, trades.csv 저장
```

백테스터는 실시간 엔진과 동일한 `Trader`/`RiskManager`/`PaperBroker` 를 사용하며 캔들 고저로 손절·익절 도달을 판정합니다
(같은 봉에서 둘 다 닿으면 손절 우선의 보수적 가정). 지표는 종목별로 한 번에 벡터 계산합니다.

## 프로젝트 구조

```
KDayTrader.bat / .command / .sh   원클릭 실행 (그래픽 앱)
launcher.py                  의존성 설치 → 앱 서버 → 브라우저 열기
main.py                      CLI (run / demo / backtest / screen / news / analyze, 인자 없이 실행 시 메뉴)
start.sh / start.bat         CLI 메뉴 실행 스크립트
kdaytrader/
  market.py                  장 시간, 호가단위, 수수료·세금
  indicators.py              기술적 지표 (numpy/pandas)
  strategy/ensemble.py       앙상블 전략·국면 가중치·임계값
  strategy/quant.py          퀀트 알파 (페어, 마이크로스트럭처, 모멘텀, 공식형 알파, 변동성 국면)
  context.py                 거시 지수·섹터·뉴스 감성·이벤트 → 점수 보정/진입 차단/긴급 청산
  risk.py                    포지션 사이징, 손절/익절/트레일링, 일일 한도, 켈리
  trader.py                  전략·리스크·브로커 연결 (엔진/백테스터 공용)
  engine.py                  실시간 비동기 엔진 + 대시보드
  backtest.py                백테스터, 그리드 탐색
  analytics.py               gs-quant 참고 시계열 분석 (z-score, 변동성, 베타, 낙폭, 바스켓, 이벤트 스터디)
  strategy/rules.py          gs-quant 식 트리거/액션 규칙 엔진
  app.py                     그래픽 앱 백엔드 (엔진 수명주기, 설정 편집, 백테스트 작업, 차트/뉴스/수익률/진단 API)
  web/                       그래픽 앱 프런트엔드 (index.html, app.js, app.css)
  factory.py                 설정 → 피드/브로커/엔진 조립 (CLI·앱 공용)
  performance.py             수익률 관리 (거래 CSV 누적, 일/월/종목/사유/시간대 통계)
  diagnostics.py             인터넷 데이터 소스 연결 진단
  webui.py / webui.html      CLI 용 간이 브라우저 대시보드
  screener.py                거래량 상위 스크리너
  dashboard.py               Rich 대시보드
  notifier.py                콘솔/텔레그램 알림
  config.py                  YAML 설정 로더
  data/base.py               Tick/Candle/피드 인터페이스
  data/candles.py            틱 → 분봉 집계
  data/kis.py                한국투자증권 REST/웹소켓 클라이언트
  data/naver.py              네이버 금융 폴링/차트/거래량 상위
  data/news.py               RSS 뉴스·지수 수집기
  data/sim.py                시뮬레이션 피드
  broker/paper.py            페이퍼 브로커
  broker/kis.py              한국투자증권 주문 브로커
tests/                       pytest (지표, 캔들, 리스크, 브로커, 전략, 백테스트, 컨텍스트, 퀀트, 파서)
```

## 테스트

```bash
python -m pytest -q
```

## 주의사항

- 네이버 금융 엔드포인트는 비공식이며 예고 없이 바뀔 수 있습니다. 폴링 주기를 1초 미만으로 낮추지 마세요.
- 한국투자증권 API 는 초당 호출 제한(실전 20회, 모의 2회)이 있으며 분봉 조회는 당일분만 제공합니다.
- 공휴일 캘린더는 포함되어 있지 않습니다(주말만 제외).
- 뉴스 감성은 사전 기반의 단순 모델입니다. 오탐이 있을 수 있으니 임계값(`context.*`)을 보수적으로 두세요.
