from datetime import datetime, time

import pytest

from kdaytrader.broker.paper import PaperBroker
from kdaytrader.market import KST, FeeModel
from kdaytrader.risk import RiskManager, RiskParams


def _now(h=10, m=0):
    return datetime(2026, 9, 23, h, m, tzinfo=KST)


def test_paper_buy_sell_with_fees_and_slippage():
    b = PaperBroker(1_000_000, FeeModel(0.00015, 0.0015), slippage_ticks=1)
    fill = b.buy("005930", 10, 70_000, _now())
    assert fill.price == 70_100  # 1틱 슬리피지
    assert b.position("005930").qty == 10
    assert b.cash() < 1_000_000 - 701_000
    fill2 = b.sell("005930", 10, 71_000, _now(11))
    assert fill2.price == 70_900
    assert b.position("005930") is None
    t = b.trades()[0]
    assert t.pnl == pytest.approx((70_900 - 70_100) * 10 - fill2.fee - 70_100 * 10 * 0.00015)


def test_paper_buy_caps_qty_to_cash():
    b = PaperBroker(100_000, slippage_ticks=0)
    fill = b.buy("005930", 100, 70_000, _now())
    assert fill.qty == 1


def test_position_size_respects_risk_and_alloc():
    rm = RiskManager(RiskParams(risk_per_trade=0.01, max_position_pct=0.3, atr_stop_mult=1.5, min_stop_pct=0.006, max_stop_pct=0.03))
    equity, cash, price, atr = 10_000_000, 10_000_000, 70_000, 500
    qty = rm.position_size(equity, cash, price, atr)
    dist = max(atr * 1.5, price * 0.006)
    assert qty == min(int((equity * 0.01) // dist), int((equity * 0.3) // price))
    assert qty * price <= equity * 0.3
    assert rm.position_size(equity, cash, price, 5_000, scale=0.5) == int((equity * 0.005) // (price * 0.03))
    assert rm.position_size(equity, 100_000, price, atr) == 1


def test_stop_distance_clamped():
    rm = RiskManager(RiskParams(min_stop_pct=0.006, max_stop_pct=0.03, atr_stop_mult=1.5))
    assert rm.stop_distance(100_000, 10) == pytest.approx(600)
    assert rm.stop_distance(100_000, 10_000) == pytest.approx(3_000)


def test_can_enter_time_and_limits():
    rm = RiskManager(RiskParams(max_positions=2, daily_loss_limit_pct=0.03, entry_start=time(9, 5), entry_end=time(14, 50)))
    rm.new_day(_now().date(), 10_000_000)
    assert rm.can_enter(_now(9, 2), "A", 0)[0] is False
    assert rm.can_enter(_now(15, 0), "A", 0)[0] is False
    assert rm.can_enter(_now(10), "A", 2)[0] is False
    assert rm.can_enter(_now(10), "A", 1)[0] is True
    rm.record_trade("A", -400_000, _now(10, 5))
    ok, why = rm.can_enter(_now(10, 30), "B", 0)
    assert not ok and "손실 한도" in why
    rm.new_day(_now().date(), 10_000_000)
    rm.record_trade("A", -1_000, _now(10, 5))
    assert rm.can_enter(_now(10, 10), "A", 0)[0] is False  # 쿨다운
    assert rm.can_enter(_now(10, 20), "A", 0)[0] is True


def test_exit_rules_and_trailing():
    rm = RiskManager(RiskParams(atr_stop_mult=1.0, min_stop_pct=0.001, max_stop_pct=0.1, take_profit_r=2.0, breakeven_r=1.0, trail_atr_mult=1.0, force_close=time(15, 12)))
    b = PaperBroker(10_000_000, slippage_ticks=0)
    b.buy("A", 10, 100_000, _now())
    pos = b.position("A")
    rm.attach(pos, atr=1_000)
    assert pos.stop_price == 99_000 and pos.take_profit == 102_000
    assert rm.check_exit(pos, 99_500, _now(10, 1))[0] is None
    assert rm.check_exit(pos, 98_900, _now(10, 1))[0] == "손절"
    assert rm.check_exit(pos, 100_500, _now(10, 1), low=100_200, high=102_100)[0] == "익절"
    assert rm.check_exit(pos, 100_500, _now(15, 12))[0] == "장 마감 강제 청산"
    # 1R 도달 → 본전 이동, 고점 대비 ATR 트레일
    rm.update_trailing(pos, 101_000)
    assert pos.breakeven_moved and pos.stop_price >= 100_000
    rm.update_trailing(pos, 101_800)
    assert pos.stop_price == 100_800
    assert rm.check_exit(pos, 100_700, _now(10, 5))[0] == "본전/트레일링 스탑"


def test_partial_take_once():
    rm = RiskManager(RiskParams(partial_take_r=1.5, partial_take_ratio=0.5, min_stop_pct=0.001, max_stop_pct=0.1, atr_stop_mult=1.0))
    b = PaperBroker(10_000_000, slippage_ticks=0)
    b.buy("A", 10, 100_000, _now())
    pos = b.position("A")
    rm.attach(pos, atr=1_000)
    assert rm.partial_take_qty(pos, 101_000) == 0
    assert rm.partial_take_qty(pos, 101_600) == 5
    assert rm.partial_take_qty(pos, 102_000) == 0
