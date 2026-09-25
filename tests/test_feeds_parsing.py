from datetime import datetime

from kdaytrader.data.kis import parse_realtime
from kdaytrader.data.naver import parse_fchart, resample
from kdaytrader.data.news import parse_rss
from kdaytrader.data.base import Candle
from kdaytrader.market import KST


def test_parse_kis_realtime_message():
    fields = ["005930", "093015", "71000", "2", "500", "0.71", "70900", "70500", "71200", "70300", "71100", "71000", "150", "1234567"]
    fields += ["0"] * (46 - len(fields))
    msg = "0|H0STCNT0|001|" + "^".join(fields)
    ticks = parse_realtime(msg)
    assert len(ticks) == 1
    t = ticks[0]
    assert t.code == "005930" and t.price == 71000 and t.volume == 150 and t.acc_volume == 1234567
    assert t.ts.hour == 9 and t.ts.minute == 30 and t.ts.second == 15
    assert parse_realtime('{"header":{"tr_id":"PINGPONG"}}') == []


def test_parse_fchart_minute_and_day():
    xml = '<protocol><chartdata><item data="202609230901|70000|70100|69900|70050|1200" /><item data="202609230902|70050|70200|70000|70150|900" /></chartdata></protocol>'
    cs = parse_fchart(xml, "minute")
    assert len(cs) == 2
    assert cs[0].ts == datetime(2026, 9, 23, 9, 0, tzinfo=KST)  # 종료시각 → 시작시각 보정
    assert cs[1].close == 70150 and cs[1].volume == 900
    day = parse_fchart('<x><item data="20260923|1|2|0.5|1.5|10"/></x>', "day")
    assert day[0].ts.date() == datetime(2026, 9, 23).date()


def test_resample_to_5min():
    base = datetime(2026, 9, 23, 9, 0, tzinfo=KST)
    from datetime import timedelta

    cs = [Candle(base + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(12)]
    out = resample(cs, 5)
    assert len(out) == 3
    assert out[0].open == 100 and out[0].close == 104.5 and out[0].high == 105 and out[0].low == 99 and out[0].volume == 50
    assert out[2].volume == 20


def test_parse_rss():
    xml = """<?xml version="1.0"?><rss><channel>
    <item><title>코스피 급등, 외국인 순매수</title><link>http://x/1</link><pubDate>Wed, 23 Sep 2026 01:00:00 GMT</pubDate></item>
    <item><title>무제</title></item></channel></rss>"""
    items = parse_rss(xml, "t")
    assert len(items) == 2
    ts, title, link = items[0]
    assert title.startswith("코스피") and link == "http://x/1"
    assert ts.tzinfo is not None and ts.hour == 10  # GMT 01:00 → KST 10:00
