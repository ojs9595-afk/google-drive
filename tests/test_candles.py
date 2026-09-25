from datetime import datetime, timedelta

from kdaytrader.data.base import Tick
from kdaytrader.data.candles import CandleBuilder, CandleStore
from kdaytrader.market import KST


def _t(minute: int, sec: int, price: float, vol: int = 10, acc: int = 0):
    return Tick("005930", datetime(2026, 9, 23, 9, minute, sec, tzinfo=KST), price, vol, acc)


def test_builder_closes_candle_on_new_minute():
    b = CandleBuilder(60)
    assert b.add_tick(_t(0, 5, 100)) is None
    assert b.add_tick(_t(0, 30, 105)) is None
    assert b.add_tick(_t(0, 50, 98)) is None
    closed = b.add_tick(_t(1, 1, 99))
    assert closed is not None
    assert (closed.open, closed.high, closed.low, closed.close, closed.volume) == (100, 105, 98, 98, 30)
    assert len(b) == 1
    assert b.current.open == 99


def test_builder_uses_acc_volume_diff_when_no_trade_volume():
    b = CandleBuilder(60)
    b.add_tick(_t(0, 0, 100, vol=0, acc=1000))
    b.add_tick(_t(0, 10, 101, vol=0, acc=1300))
    b.add_tick(_t(0, 20, 102, vol=0, acc=1350))
    closed = b.add_tick(_t(1, 0, 102, vol=0, acc=1400))
    assert closed.volume == 350


def test_builder_ignores_late_ticks():
    b = CandleBuilder(60)
    b.add_tick(_t(1, 0, 100))
    assert b.add_tick(_t(0, 30, 90)) is None
    assert b.current.low == 100


def test_store_df_window():
    s = CandleStore(60)
    for i in range(10):
        s.add_tick(_t(i, 0, 100 + i))
    df = s.df("005930", n=3)
    assert len(df) == 3
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert s.last_price["005930"] == 109
