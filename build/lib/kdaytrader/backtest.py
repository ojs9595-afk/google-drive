"""분봉 기반 백테스터. 실시간 엔진과 같은 Trader/RiskManager/PaperBroker 를 사용한다."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from . import analytics
from .broker.base import Trade
from .broker.paper import PaperBroker
from .data.base import Candle
from .context import MarketContext
from .data.base import candles_to_df
from .data.candles import CandleStore
from .indicators import compute_all
from .market import FeeModel
from .notifier import Notifier
from .risk import RiskManager, RiskParams
from .strategy.base import Strategy
from .strategy.ensemble import EnsembleStrategy, StrategyParams
from .strategy.quant import QuantParams, QuantSignals
from .strategy.rules import RuleSet
from .trader import Trader


@dataclass
class BacktestResult:
    initial_cash: float
    final_equity: float
    trades: list[Trade]
    equity_curve: pd.Series
    daily_returns: pd.Series
    signals: int = 0

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.initial_cash - 1.0) * 100.0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl > 0) / len(self.trades) * 100.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl <= 0)
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def avg_win(self) -> float:
        w = [t.pnl for t in self.trades if t.pnl > 0]
        return float(np.mean(w)) if w else 0.0

    @property
    def avg_loss(self) -> float:
        l = [t.pnl for t in self.trades if t.pnl <= 0]
        return float(np.mean(l)) if l else 0.0

    @property
    def expectancy(self) -> float:
        return float(np.mean([t.pnl for t in self.trades])) if self.trades else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        peak = self.equity_curve.cummax()
        dd = (self.equity_curve / peak - 1.0) * 100.0
        return float(dd.min())

    @property
    def sharpe(self) -> float:
        r = self.daily_returns
        if len(r) < 2 or r.std() == 0:
            return 0.0
        return float(r.mean() / r.std() * math.sqrt(252))

    @property
    def sortino(self) -> float:
        return analytics.sortino_ratio(self.daily_returns)

    @property
    def calmar(self) -> float:
        return analytics.calmar_ratio(self.total_return_pct, self.max_drawdown_pct, max(1, len(self.daily_returns)))

    @property
    def max_drawdown_duration_bars(self) -> int:
        return analytics.drawdown_duration(self.equity_curve) if not self.equity_curve.empty else 0

    @property
    def exp_volatility_pct(self) -> float:
        """지수가중 실현변동성(일간 기준 연율화 %, gs-quant exponential_volatility)."""
        if self.equity_curve.empty:
            return 0.0
        daily = self.equity_curve.groupby(self.equity_curve.index.date).last()
        daily.index = pd.DatetimeIndex(daily.index)
        v = analytics.exponential_volatility(daily, 0.75, intraday=False).dropna()
        return float(v.iloc[-1]) if not v.empty else 0.0

    @property
    def avg_holding_minutes(self) -> float:
        return float(np.mean([t.holding_minutes for t in self.trades])) if self.trades else 0.0

    def summary(self) -> dict:
        return {
            "초기자금": self.initial_cash,
            "최종자산": round(self.final_equity),
            "총수익률(%)": round(self.total_return_pct, 2),
            "거래횟수": self.n_trades,
            "승률(%)": round(self.win_rate, 1),
            "손익비(PF)": round(self.profit_factor, 2) if math.isfinite(self.profit_factor) else "inf",
            "평균수익": round(self.avg_win),
            "평균손실": round(self.avg_loss),
            "기대손익/거래": round(self.expectancy),
            "최대낙폭(%)": round(self.max_drawdown_pct, 2),
            "샤프(일간)": round(self.sharpe, 2),
            "소르티노": round(self.sortino, 2),
            "칼마": round(self.calmar, 2),
            "낙폭지속(봉)": self.max_drawdown_duration_bars,
            "지수가중변동성(%)": round(self.exp_volatility_pct, 2),
            "평균보유(분)": round(self.avg_holding_minutes, 1),
            "시그널수": self.signals,
        }


class Backtester:
    def __init__(
        self,
        strategy: Strategy | None = None,
        risk_params: RiskParams | None = None,
        initial_cash: float = 10_000_000,
        fee: FeeModel | None = None,
        slippage_ticks: int = 1,
        interval_min: int = 1,
        warmup_bars: int | None = None,
        context: MarketContext | None = None,
        window: int = 420,
        quant_params: QuantParams | None = None,
        pairs: dict[str, str] | None = None,
        rules: RuleSet | None = None,
    ):
        self.context = context
        self.window = window
        self.quant_params = quant_params
        self.pairs = pairs
        self.rules = rules
        self.strategy = strategy or EnsembleStrategy(StrategyParams())
        self.risk_params = risk_params or RiskParams()
        self.initial_cash = initial_cash
        self.fee = fee or FeeModel()
        self.slippage_ticks = slippage_ticks
        self.interval_min = interval_min
        self.warmup_bars = warmup_bars if warmup_bars is not None else self.strategy.min_bars

    def run(self, data: dict[str, list[Candle]], names: dict[str, str] | None = None) -> BacktestResult:
        """data: {종목코드: 시간순 캔들 리스트}. 여러 종목을 시각 순서로 병합해 재생한다."""
        broker = PaperBroker(self.initial_cash, self.fee, self.slippage_ticks)
        risk = RiskManager(self.risk_params)
        store = CandleStore(self.interval_min * 60, maxlen=5000)
        ctx = self.context or MarketContext()
        qp = self.quant_params or QuantParams()
        quant = QuantSignals(store, qp, self.pairs, ctx.stock_sectors) if qp.enabled else None
        trader = Trader(self.strategy, risk, broker, store, Notifier(console=False), names or {}, auto_trade=True, window=self.window, context=ctx, quant=quant, rules=self.rules)

        # 지표는 종목별로 한 번에 벡터 계산 (모든 지표가 인과적이므로 결과 동일)
        ind_params = getattr(getattr(self.strategy, "p", None), "indicator_params", lambda: None)()
        precomputed: dict[str, pd.DataFrame] = {}
        events: list[tuple[datetime, str, int, Candle]] = []
        for code, candles in data.items():
            if ind_params is not None and hasattr(self.strategy, "evaluate_with_indicators"):
                precomputed[code] = compute_all(candles_to_df(candles), ind_params)
            for i, c in enumerate(candles):
                if i < self.warmup_bars:
                    store.add_candle(code, c)
                else:
                    events.append((c.ts, code, i, c))
        events.sort(key=lambda e: e[0])

        equity_points: list[tuple[datetime, float]] = []
        signals = 0
        last_day = None
        for ts, code, i, c in events:
            if last_day is not None and ts.date() != last_day:
                # 날짜가 바뀌면 전날 마감 청산 (안전장치)
                trader.force_close_all(ts, "일자 변경 청산")
            last_day = ts.date()
            risk.ensure_day(ts, broker.equity(store.last_price))
            # 1) 캔들 내 고저로 손절/익절 체크 (보유 중일 때)
            if broker.position(code) is not None:
                trader.on_price(code, c.close, ts, low=c.low, high=c.high, open_=c.open)
            # 2) 캔들 반영 후 전략 평가
            store.add_candle(code, c)
            ind = None
            if code in precomputed:
                ind = precomputed[code].iloc[max(0, i + 1 - self.window) : i + 1]
            sig = trader.on_candle(code, ts, ind)
            if sig is not None and sig.action.value != "HOLD":
                signals += 1
            equity_points.append((ts, broker.equity(store.last_price)))
        if events:
            trader.force_close_all(events[-1][0], "백테스트 종료 청산")
            equity_points.append((events[-1][0], broker.equity(store.last_price)))

        curve = pd.Series([e for _, e in equity_points], index=pd.DatetimeIndex([t for t, _ in equity_points])) if equity_points else pd.Series(dtype=float)
        if not curve.empty:
            daily = curve.groupby(curve.index.date).last()
            daily_ret = daily.pct_change().dropna()
            daily_ret = pd.concat([pd.Series([daily.iloc[0] / self.initial_cash - 1.0]), daily_ret]) if len(daily) else daily_ret
        else:
            daily_ret = pd.Series(dtype=float)
        return BacktestResult(self.initial_cash, broker.equity(store.last_price), list(broker.trades()), curve, daily_ret, signals)


def grid_search(data: dict[str, list[Candle]], grid: dict[str, list], base: StrategyParams | None = None, risk_params: RiskParams | None = None, **kw) -> list[tuple[dict, dict]]:
    """간단한 파라미터 그리드 탐색. 결과를 총수익률 내림차순으로 반환."""
    import itertools
    from dataclasses import replace

    base = base or StrategyParams()
    keys = list(grid.keys())
    results = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        overrides = dict(zip(keys, combo))
        params = replace(base, **overrides)
        bt = Backtester(EnsembleStrategy(params), risk_params, **kw)
        res = bt.run(data)
        results.append((overrides, res.summary()))
    results.sort(key=lambda r: r[1]["총수익률(%)"], reverse=True)
    return results
