"""수익률 관리: 거래 CSV(logs/trades_*.csv)와 현재 세션 거래를 합쳐 일/월/종목/시간대/사유별 성과를 계산한다."""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .market import KST

CSV_HEADER = ["진입시각", "청산시각", "코드", "종목", "수량", "진입가", "청산가", "손익", "손익%", "비용", "사유"]


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)


def load_trade_files(log_dir: str | Path) -> list[dict]:
    out = []
    d = Path(log_dir)
    if not d.exists():
        return out
    for f in sorted(d.glob("trades_*.csv")):
        try:
            with f.open(encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    try:
                        out.append(
                            {
                                "entry_ts": _parse_ts(r["진입시각"]), "exit_ts": _parse_ts(r["청산시각"]), "code": r["코드"], "name": r.get("종목") or r["코드"],
                                "qty": int(float(r["수량"])), "entry_price": float(r["진입가"]), "exit_price": float(r["청산가"]), "pnl": float(r["손익"]),
                                "pnl_pct": float(r["손익%"]), "fees": float(r.get("비용") or 0), "reason": r.get("사유", ""), "source": f.name,
                            }
                        )
                    except (KeyError, ValueError):
                        continue
        except OSError:
            continue
    return out


def trades_from_broker(trades, names: dict[str, str] | None = None) -> list[dict]:
    names = names or {}
    return [
        {
            "entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "code": t.code, "name": t.name or names.get(t.code) or t.code, "qty": t.qty,
            "entry_price": t.entry_price, "exit_price": t.exit_price, "pnl": t.pnl, "pnl_pct": t.pnl_pct, "fees": t.fees, "reason": t.reason, "source": "session",
        }
        for t in trades
    ]


def merge_trades(*lists: list[dict]) -> list[dict]:
    """중복(같은 코드·진입·청산 시각·수량) 제거 후 청산 시각 순 정렬."""
    seen = set()
    out = []
    for lst in lists:
        for t in lst:
            key = (t["code"], t["entry_ts"].strftime("%Y%m%d%H%M%S"), t["exit_ts"].strftime("%Y%m%d%H%M%S"), t["qty"], round(t["exit_price"]))
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
    out.sort(key=lambda t: t["exit_ts"])
    return out


def _streaks(pnls: list[float]) -> tuple[int, int, int]:
    best_w = best_l = cur = 0
    cur_sign = 0
    for p in pnls:
        sgn = 1 if p > 0 else -1
        cur = cur + 1 if sgn == cur_sign else 1
        cur_sign = sgn
        if sgn > 0:
            best_w = max(best_w, cur)
        else:
            best_l = max(best_l, cur)
    return best_w, best_l, (cur * cur_sign if pnls else 0)


def performance_report(trades: list[dict], days: int | None = None, initial_cash: float = 10_000_000, now: datetime | None = None) -> dict:
    now = now or datetime.now(tz=KST)
    if days:
        cutoff = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        trades = [t for t in trades if t["exit_ts"] >= cutoff]
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    # 일별
    daily = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "fees": 0.0})
    for t in trades:
        d = daily[t["exit_ts"].strftime("%Y-%m-%d")]
        d["pnl"] += t["pnl"]
        d["trades"] += 1
        d["wins"] += 1 if t["pnl"] > 0 else 0
        d["fees"] += t["fees"]
    daily_rows = []
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    dd_days = 0
    cur_dd_days = 0
    for day in sorted(daily):
        d = daily[day]
        cum += d["pnl"]
        peak = max(peak, cum)
        dd = cum - peak
        if dd < 0:
            cur_dd_days += 1
            dd_days = max(dd_days, cur_dd_days)
        else:
            cur_dd_days = 0
        max_dd = min(max_dd, dd)
        daily_rows.append({"date": day, "pnl": round(d["pnl"]), "trades": d["trades"], "win_rate": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0, "cum": round(cum), "fees": round(d["fees"]), "ret_pct": round(d["pnl"] / initial_cash * 100, 3)})
    # 월별 / 주별
    monthly = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "days": set()})
    weekly = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for t in trades:
        m = monthly[t["exit_ts"].strftime("%Y-%m")]
        m["pnl"] += t["pnl"]
        m["trades"] += 1
        m["wins"] += 1 if t["pnl"] > 0 else 0
        m["days"].add(t["exit_ts"].date())
        iso = t["exit_ts"].isocalendar()
        w = weekly[f"{iso[0]}-W{iso[1]:02d}"]
        w["pnl"] += t["pnl"]
        w["trades"] += 1
        w["wins"] += 1 if t["pnl"] > 0 else 0
    monthly_rows = [{"month": k, "pnl": round(v["pnl"]), "trades": v["trades"], "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0, "days": len(v["days"]), "ret_pct": round(v["pnl"] / initial_cash * 100, 2)} for k, v in sorted(monthly.items())]
    weekly_rows = [{"week": k, "pnl": round(v["pnl"]), "trades": v["trades"], "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0} for k, v in sorted(weekly.items())]
    # 종목별 / 사유별 / 시간대별
    def group(keyfn, label):
        g = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "name": ""})
        for t in trades:
            k, nm = keyfn(t)
            x = g[k]
            x["pnl"] += t["pnl"]
            x["trades"] += 1
            x["wins"] += 1 if t["pnl"] > 0 else 0
            x["name"] = nm
        rows = [{label: k, "name": v["name"], "pnl": round(v["pnl"]), "trades": v["trades"], "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0, "avg": round(v["pnl"] / v["trades"]) if v["trades"] else 0} for k, v in g.items()]
        rows.sort(key=lambda r: -r["pnl"])
        return rows

    per_code = group(lambda t: (t["code"], t["name"]), "code")
    per_reason = group(lambda t: (t["reason"].split(":")[0].strip() or "기타", ""), "reason")
    per_hour = group(lambda t: (f"{t['entry_ts'].hour:02d}시", ""), "hour")
    per_hour.sort(key=lambda r: r["hour"])
    best_w, best_l, cur_streak = _streaks(pnls)
    holding = [(t["exit_ts"] - t["entry_ts"]).total_seconds() / 60 for t in trades]
    kpi = {
        "total_pnl": round(sum(pnls)), "trades": len(trades), "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else (None if not wins else 99.0),
        "avg_win": round(gross_w / len(wins)) if wins else 0, "avg_loss": round(-gross_l / len(losses)) if losses else 0,
        "expectancy": round(sum(pnls) / len(pnls)) if pnls else 0, "max_drawdown": round(max_dd), "dd_days": dd_days,
        "best_trade": round(max(pnls)) if pnls else 0, "worst_trade": round(min(pnls)) if pnls else 0,
        "best_streak": best_w, "worst_streak": best_l, "current_streak": cur_streak,
        "avg_holding_min": round(sum(holding) / len(holding), 1) if holding else 0, "fees": round(sum(t["fees"] for t in trades)),
        "trading_days": len(daily), "pnl_per_day": round(sum(pnls) / len(daily)) if daily else 0,
        "return_pct": round(sum(pnls) / initial_cash * 100, 2) if initial_cash else 0,
        "profitable_days": sum(1 for d in daily.values() if d["pnl"] > 0),
    }
    recent = [
        {"entry": t["entry_ts"].strftime("%m-%d %H:%M"), "exit": t["exit_ts"].strftime("%H:%M"), "code": t["code"], "name": t["name"], "qty": t["qty"], "entry_price": t["entry_price"], "exit_price": t["exit_price"], "pnl": round(t["pnl"]), "pnl_pct": round(t["pnl_pct"], 2), "reason": t["reason"], "minutes": round((t["exit_ts"] - t["entry_ts"]).total_seconds() / 60), "source": t["source"]}
        for t in trades[-300:][::-1]
    ]
    return {"kpi": kpi, "daily": daily_rows, "weekly": weekly_rows[-26:], "monthly": monthly_rows, "per_code": per_code, "per_reason": per_reason, "per_hour": per_hour, "trades": recent, "days": days, "initial_cash": initial_cash}


def trades_to_csv(trades: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for t in trades:
        w.writerow([t["entry_ts"].strftime("%Y-%m-%d %H:%M:%S"), t["exit_ts"].strftime("%Y-%m-%d %H:%M:%S"), t["code"], t["name"], t["qty"], t["entry_price"], t["exit_price"], round(t["pnl"]), round(t["pnl_pct"], 3), round(t["fees"]), t["reason"]])
    return buf.getvalue()
