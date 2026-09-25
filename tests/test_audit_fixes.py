"""감사에서 확인된 결함에 대한 회귀 테스트."""
from datetime import datetime, time, timedelta

from kdaytrader.broker.kis import KISBroker
from kdaytrader.broker.paper import PaperBroker
from kdaytrader.data.base import Candle, Tick
from kdaytrader.data.candles import CandleBuilder
from kdaytrader.data.candles import CandleStore
from kdaytrader.data.sim import SimFeed
from kdaytrader.market import KST
from kdaytrader.notifier import Notifier
from kdaytrader.risk import RiskManager, RiskParams
from kdaytrader.strategy import EnsembleStrategy
from kdaytrader.trader import Trader


def _now(h=10, m=0):
    return datetime(2026, 9, 23, h, m, tzinfo=KST)


def test_candle_builder_continues_seeded_open_bar():
    b = CandleBuilder(60)
    ts = datetime(2026, 9, 23, 9, 5, tzinfo=KST)
    b.seed([Candle(ts - timedelta(minutes=1), 100, 101, 99, 100.5, 10), Candle(ts, 100.5, 100.8, 100.2, 100.6, 5)])
    assert b.add_tick(Tick("A", ts + timedelta(seconds=30), 101.0, 3)) is None
    assert len(b) == 1 and b.current is not None and b.current.ts == ts and b.current.high == 101.0 and b.current.close == 101.0
    closed = b.add_tick(Tick("A", ts + timedelta(minutes=1), 101.2, 2))
    assert closed is not None and closed.ts == ts and len(b) == 2
    assert [c.ts for c in b.candles] == [ts - timedelta(minutes=1), ts]  # 중복 타임스탬프 없음
    # 시드 이전 구간의 늦은 틱은 무시
    b2 = CandleBuilder(60)
    b2.seed([Candle(ts, 1, 1, 1, 1, 1)])
    assert b2.add_tick(Tick("A", ts - timedelta(minutes=5), 2, 1)) is None and b2.current is None


def test_gap_through_stop_fills_at_open():
    rm = RiskManager(RiskParams(atr_stop_mult=1.0, min_stop_pct=0.001, max_stop_pct=0.1, take_profit_r=2.0))
    b = PaperBroker(10_000_000, slippage_ticks=0)
    b.buy("A", 10, 100_000, _now())
    pos = b.position("A")
    rm.attach(pos, atr=1_000)  # 손절 99,000 / 익절 102,000
    reason, fill = rm.check_exit(pos, 97_500, _now(10, 1), low=97_000, high=98_000, open_=97_800)
    assert reason == "손절" and fill == 97_800  # 갭 하락 시가 체결
    reason, fill = rm.check_exit(pos, 98_900, _now(10, 1), low=98_800, high=99_500, open_=99_400)
    assert reason == "손절" and fill == 99_000  # 봉 내 도달은 손절가 체결
    reason, fill = rm.check_exit(pos, 103_000, _now(10, 1), low=102_500, high=103_500, open_=102_600)
    assert reason == "익절" and fill == 102_600


def test_kelly_scale_guards_zero_loss():
    rm = RiskManager(RiskParams(risk_per_trade=0.01))
    rm.new_day(_now().date(), 1_000_000)
    for i in range(12):
        rm.record_trade("A", 1000 if i % 2 else 0.0, _now())  # 손실 0원 거래는 손실로 세지 않음
    assert rm.kelly_scale() == RiskParams().kelly_max_scale
    rm2 = RiskManager(RiskParams(risk_per_trade=0.0))
    rm2.new_day(_now().date(), 1_000_000)
    for i in range(12):
        rm2.record_trade("A", 1000 if i % 3 else -500, _now())
    assert rm2.kelly_scale() == RiskParams().kelly_max_scale


def _trader(auto: bool):
    store = CandleStore(60)
    broker = PaperBroker(10_000_000, slippage_ticks=0)
    risk = RiskManager(RiskParams())
    notifier = Notifier(console=False)
    t = Trader(EnsembleStrategy(), risk, broker, store, notifier, {"A": "테스트"}, auto_trade=auto)
    broker.buy("A", 10, 10_000, _now())
    store.last_price["A"] = 10_100
    return t, broker, notifier


def test_force_close_respects_signal_only_mode():
    t, broker, notifier = _trader(auto=False)
    assert t.force_close_all(_now(15, 12)) == 0
    assert broker.position("A") is not None and any("강제청산 신호" in h for h in notifier.history)
    n_before = len(notifier.history)
    t.force_close_all(_now(15, 13))  # 같은 날 같은 사유는 한 번만 알림
    assert len(notifier.history) == n_before
    assert t.force_close_all(_now(15, 14), manual=True) == 1  # 사용자가 직접 요청하면 청산
    assert broker.position("A") is None


def test_force_close_auto_mode_and_alert_dedupe():
    t, broker, notifier = _trader(auto=True)
    assert t.force_close_all(_now(15, 12)) == 1 and broker.position("A") is None
    t2, broker2, notifier2 = _trader(auto=False)
    pos = broker2.position("A")
    pos.stop_price = 10_050
    for _ in range(5):
        t2.on_price("A", 10_000, _now(10, 1))
    assert sum("청산 신호" in h for h in notifier2.history) == 1  # 틱마다 반복 알림 없음


class _FakeKIS:
    paper = True

    def __init__(self, holdings):
        self.holdings = holdings
        self.orders = []

    def balance(self):
        return {"output1": [{"pdno": c, "hldg_qty": str(q), "pchs_avg_pric": "1000", "prdt_name": c} for c, q in self.holdings.items()] + [{"pdno": "BAD", "hldg_qty": ""}],
                "output2": [{"dnca_tot_amt": "5000000", "prvs_rcdl_excc_amt": "4000000"}]}

    def order(self, code, qty, side, price=0.0):
        self.orders.append((code, qty, side))
        return {"output": {"ODNO": "1"}}


def test_kis_broker_separates_account_holdings_from_managed_positions(tmp_path):
    client = _FakeKIS({"005930": 100, "000660": 5})
    b = KISBroker(client, state_path=tmp_path / "pos.json")
    assert b.cash() == 4_000_000  # 정산 반영 금액 우선
    assert set(b.holdings) == {"005930", "000660"} and b.positions() == {}  # 기존 보유분은 관리 포지션이 아님
    b.buy("000660", 3, 200_000, _now(), name="SK하이닉스")
    assert b.position("000660").qty == 3 and b.position("000660").entry_ts.tzinfo is not None
    # 재시작 후 복원 + 계좌 대조 (수량 2 로 줄어든 경우 맞춤)
    client2 = _FakeKIS({"005930": 100, "000660": 2})
    b2 = KISBroker(client2, state_path=tmp_path / "pos.json")
    assert b2.position("000660").qty == 2
    # 계좌에서 사라지면 제거
    b3 = KISBroker(_FakeKIS({"005930": 100}), state_path=tmp_path / "pos.json")
    assert b3.positions() == {}
    # 매도는 관리 수량까지만, 잔고에 avg 0 이어도 안전
    b.sell("000660", 10, 210_000, _now(10, 5))
    assert client.orders[-1] == ("000660", 3, "SELL") and b.position("000660") is None and b.trades()[-1].pnl > 0


def test_sim_index_tick_uses_closed_bar():
    feed = SimFeed(speed=0, history_days=1, seed=3)
    feed.subscribe(["005930"])
    feed.minute_candles("005930", 100)
    feed.index_minute_candles("KOSPI", 100)
    today = feed._index_today["KOSPI"]
    ts0 = today[0].ts
    v, chg = feed.index_tick(ts0)
    assert v == today[0].close  # 09:00 봉이 닫힌 시점엔 09:00 봉의 종가만 (09:01 값 아님)
    assert feed.index_tick(ts0 - timedelta(minutes=1)) is None
    # 시뮬레이션 분봉을 5분봉으로 리샘플
    five = feed.minute_candles("005930", 20, interval=5)
    assert len(five) == 20 and all((c.ts.minute % 5) == 0 for c in five)


def test_paper_cash_clamp_fills_max_affordable():
    b = PaperBroker(1_000_000, slippage_ticks=0)
    fill = b.buy("A", 1000, 10_000, _now())
    assert fill.qty == 99 and b.cash() >= 0
