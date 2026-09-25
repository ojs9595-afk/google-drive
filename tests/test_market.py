from datetime import datetime

from kdaytrader.market import KST, FeeModel, is_market_open, round_to_tick, tick_size


def test_tick_size_table():
    assert tick_size(1_500) == 1
    assert tick_size(3_000) == 5
    assert tick_size(15_000) == 10
    assert tick_size(30_000) == 50
    assert tick_size(150_000) == 100
    assert tick_size(300_000) == 500
    assert tick_size(700_000) == 1_000


def test_round_to_tick_directions():
    assert round_to_tick(72_340) == 72_300
    assert round_to_tick(72_340, "up") == 72_400
    assert round_to_tick(72_340, "down") == 72_300
    assert round_to_tick(0) == 0


def test_market_open_hours():
    assert is_market_open(datetime(2026, 9, 23, 10, 0, tzinfo=KST))  # 수요일
    assert not is_market_open(datetime(2026, 9, 23, 15, 30, tzinfo=KST))
    assert not is_market_open(datetime(2026, 9, 26, 10, 0, tzinfo=KST))  # 토요일


def test_fee_model():
    f = FeeModel(0.00015, 0.0015)
    assert abs(f.buy_cost(10_000, 10) - 15.0) < 1e-9
    assert abs(f.sell_cost(10_000, 10) - 165.0) < 1e-9
