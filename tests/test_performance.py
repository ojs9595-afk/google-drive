from datetime import datetime, timedelta
from pathlib import Path

from kdaytrader.broker.base import Trade
from kdaytrader.market import KST
from kdaytrader.performance import load_trade_files, merge_trades, performance_report, trades_from_broker, trades_to_csv


def _t(day: int, hour: int, pnl: float, code="005930", name="삼성전자", qty=10, minutes=20, reason="익절"):
    entry = datetime(2026, 9, day, hour, 0, tzinfo=KST)
    return {"entry_ts": entry, "exit_ts": entry + timedelta(minutes=minutes), "code": code, "name": name, "qty": qty, "entry_price": 70000.0, "exit_price": 70000.0 + pnl / qty, "pnl": pnl, "pnl_pct": pnl / 700000 * 100, "fees": 100.0, "reason": reason, "source": "test"}


def test_report_kpis_daily_monthly_streaks():
    trades = [_t(1, 9, 10000), _t(1, 10, -5000, reason="손절"), _t(2, 11, 20000, code="000660", name="SK하이닉스"), _t(2, 13, 15000), _t(3, 9, -30000, reason="손절")]
    now = datetime(2026, 9, 4, tzinfo=KST)
    r = performance_report(trades, days=None, initial_cash=10_000_000, now=now)
    k = r["kpi"]
    assert k["total_pnl"] == 10000 and k["trades"] == 5 and k["win_rate"] == 60.0
    assert k["profit_factor"] == round(45000 / 35000, 2)
    assert k["best_streak"] == 2 and k["worst_streak"] == 1 and k["current_streak"] == -1
    assert k["max_drawdown"] == -30000 and k["trading_days"] == 3 and k["profitable_days"] == 2
    assert [d["cum"] for d in r["daily"]] == [5000, 40000, 10000]
    assert r["monthly"][0]["month"] == "2026-09" and r["monthly"][0]["trades"] == 5 and r["monthly"][0]["days"] == 3
    assert r["per_code"][0]["code"] == "000660"  # 손익 내림차순
    assert {x["reason"] for x in r["per_reason"]} == {"익절", "손절"}
    assert r["per_hour"][0]["hour"] == "09시"
    # 기간 필터
    r2 = performance_report(trades, days=2, initial_cash=10_000_000, now=now)
    assert r2["kpi"]["trades"] == 3


def test_csv_roundtrip_and_merge(tmp_path: Path):
    trades = [_t(1, 9, 10000), _t(1, 10, -5000)]
    (tmp_path / "trades_20260901.csv").write_text(trades_to_csv(trades), encoding="utf-8-sig")
    loaded = load_trade_files(tmp_path)
    assert len(loaded) == 2 and loaded[0]["pnl"] == 10000 and loaded[0]["entry_ts"].tzinfo is not None
    # 세션 거래와 병합 시 중복 제거
    session = trades_from_broker([Trade("005930", 10, 70000.0, 71000.0, trades[0]["entry_ts"], trades[0]["exit_ts"], 10000.0, 1.43, 100.0, "익절", "삼성전자")], {})
    merged = merge_trades(loaded, session)
    assert len(merged) == 2
    new = trades_from_broker([Trade("035420", 3, 200000.0, 201000.0, trades[1]["entry_ts"], trades[1]["exit_ts"] + timedelta(minutes=5), 3000.0, 0.5, 50.0, "익절", "NAVER")], {})
    assert len(merge_trades(loaded, new)) == 3


def test_empty_report():
    r = performance_report([], days=30)
    assert r["kpi"]["trades"] == 0 and r["kpi"]["win_rate"] == 0.0 and r["daily"] == [] and r["kpi"]["profit_factor"] is None
