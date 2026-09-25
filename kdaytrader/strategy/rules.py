"""gs-quant 의 백테스트 프레임워크(Trigger → Action) 를 본뜬 규칙 엔진.

gs_quant.backtests.triggers 의 MeanReversionTrigger / RiskTrigger / PortfolioTrigger / AggregateTrigger / NotTrigger 와
actions 의 AddTradeAction / ExitPositionAction 개념을 일중 데이트레이딩용으로 단순화했다.

각 Trigger 는 (지표 DataFrame, 포지션 유무, 시각) 을 보고 TriggerInfo(발동 여부, 방향, 근거) 를 낸다.
RuleSet 은 트리거들을 평가해 앙상블 전략에 점수 보정(bias) 을 더하거나, 진입 차단 / 강제 청산을 요구한다.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum

import numpy as np
import pandas as pd

from ..analytics import zscores


class TriggerDirection(str, Enum):
    ABOVE = "above"
    BELOW = "below"
    EQUAL = "equal"


class ActionType(str, Enum):
    SCORE = "score"  # 점수 가감
    BLOCK_ENTRY = "block_entry"  # 신규 진입 금지
    EXIT = "exit"  # 보유분 청산
    ENTER = "enter"  # 진입 점수 강제 상향 (가중치 큰 SCORE)


@dataclass
class TriggerInfo:
    triggered: bool
    scaling: float = 0.0  # +1 매수 우위, -1 매도 우위
    reason: str = ""

    def __bool__(self) -> bool:
        return self.triggered


@dataclass
class Action:
    type: ActionType
    score: float = 0.0  # ActionType.SCORE 일 때 가감 점수 (scaling 을 곱함)
    name: str = ""


class Trigger(abc.ABC):
    def __init__(self, actions: list[Action] | Action | None = None, name: str = ""):
        self.actions = actions if isinstance(actions, list) else ([actions] if actions else [])
        self.name = name or type(self).__name__

    @abc.abstractmethod
    def has_triggered(self, ind: pd.DataFrame, has_position: bool, now: datetime) -> TriggerInfo:
        ...


def check_barrier(direction: TriggerDirection, value: float, level: float) -> bool:
    if value != value:  # NaN
        return False
    if direction == TriggerDirection.ABOVE:
        return value > level
    if direction == TriggerDirection.BELOW:
        return value < level
    return value == level


class MktTrigger(Trigger):
    """지표 컬럼이 기준선을 넘으면 발동 (gs-quant MktTriggerRequirements)."""

    def __init__(self, column: str, level: float, direction: TriggerDirection, scaling: float = 1.0, **kw):
        super().__init__(**kw)
        self.column, self.level, self.direction, self.scaling = column, level, direction, scaling

    def has_triggered(self, ind, has_position, now):
        if self.column not in ind.columns or len(ind) == 0:
            return TriggerInfo(False)
        v = float(ind[self.column].iloc[-1])
        if check_barrier(self.direction, v, self.level):
            return TriggerInfo(True, self.scaling, f"{self.column} {v:.2f} {self.direction.value} {self.level}")
        return TriggerInfo(False)


class MeanReversionTrigger(Trigger):
    """롤링 z-score 가 |bound| 를 넘으면 반대 방향, 평균 복귀 시 청산 (gs-quant MeanReversionTriggerRequirements)."""

    def __init__(self, column: str = "close", z_score_bound: float = 2.0, rolling_mean_window: int = 60, rolling_std_window: int = 60, **kw):
        super().__init__(**kw)
        self.column = column
        self.bound = z_score_bound
        self.mean_w = rolling_mean_window
        self.std_w = rolling_std_window
        self.current_position = 0

    def has_triggered(self, ind, has_position, now):
        s = ind[self.column] if self.column in ind.columns else None
        if s is None or len(s) < max(self.mean_w, self.std_w) + 1:
            return TriggerInfo(False)
        mean = s.rolling(self.mean_w).mean().iloc[-1]
        sd = s.rolling(self.std_w).std(ddof=1).iloc[-1]
        price = float(s.iloc[-1])
        if not sd or sd != sd:
            return TriggerInfo(False)
        z = (price - mean) / sd
        if not has_position:
            self.current_position = 0
            if abs(z) > self.bound:
                if price > mean:
                    return TriggerInfo(True, -1.0, f"평균회귀 z={z:+.1f} 고평가")
                self.current_position = 1
                return TriggerInfo(True, 1.0, f"평균회귀 z={z:+.1f} 저평가")
            return TriggerInfo(False)
        if price > mean:  # 롱 포지션이 평균에 복귀 → 청산 신호
            self.current_position = 0
            return TriggerInfo(True, -1.0, f"평균 복귀(z={z:+.1f})")
        return TriggerInfo(False)


class RiskTrigger(Trigger):
    """포지션 리스크 지표(미실현 R배수, 보유 시간 등)가 기준을 넘으면 발동 (gs-quant RiskTriggerRequirements)."""

    def __init__(self, measure: str, level: float, direction: TriggerDirection, **kw):
        super().__init__(**kw)
        self.measure, self.level, self.direction = measure, level, direction
        self.position_metrics: dict[str, float] = {}

    def has_triggered(self, ind, has_position, now):
        if not has_position or self.measure not in self.position_metrics:
            return TriggerInfo(False)
        v = self.position_metrics[self.measure]
        if check_barrier(self.direction, v, self.level):
            return TriggerInfo(True, -1.0, f"리스크 {self.measure}={v:.2f} {self.direction.value} {self.level}")
        return TriggerInfo(False)


class TimeWindowTrigger(Trigger):
    """지정 시간대에 발동 (gs-quant DateTrigger / IntradayPeriodicTrigger 의 일중 버전)."""

    def __init__(self, start: time, end: time, scaling: float = 1.0, **kw):
        super().__init__(**kw)
        self.start, self.end, self.scaling = start, end, scaling

    def has_triggered(self, ind, has_position, now):
        t = now.time()
        if self.start <= t < self.end:
            return TriggerInfo(True, self.scaling, f"시간대 {self.start:%H:%M}-{self.end:%H:%M}")
        return TriggerInfo(False)


class AggregateTrigger(Trigger):
    """여러 트리거가 모두(AND) 또는 하나라도(OR) 발동하면 발동."""

    def __init__(self, triggers: list[Trigger], mode: str = "all", **kw):
        super().__init__(**kw)
        self.triggers, self.mode = triggers, mode

    def has_triggered(self, ind, has_position, now):
        infos = [t.has_triggered(ind, has_position, now) for t in self.triggers]
        hits = [i for i in infos if i]
        ok = all(infos) if self.mode == "all" else bool(hits)
        if not ok:
            return TriggerInfo(False)
        scaling = float(np.sign(sum(i.scaling for i in hits))) if hits else 1.0
        return TriggerInfo(True, scaling, " & ".join(i.reason for i in hits))


class NotTrigger(Trigger):
    def __init__(self, trigger: Trigger, **kw):
        super().__init__(**kw)
        self.trigger = trigger

    def has_triggered(self, ind, has_position, now):
        inner = self.trigger.has_triggered(ind, has_position, now)
        return TriggerInfo(not inner.triggered, 1.0, f"not({self.trigger.name})") if not inner else TriggerInfo(False)


@dataclass
class RuleOutcome:
    bias: float = 0.0
    entry_block: str = ""
    exit_hint: str = ""
    notes: list[str] = field(default_factory=list)


class RuleSet:
    """트리거 → 액션 실행 결과를 앙상블 전략의 보정값으로 변환."""

    def __init__(self, triggers: list[Trigger] | None = None):
        self.triggers = list(triggers or [])

    def evaluate(self, ind: pd.DataFrame, has_position: bool, now: datetime, position_metrics: dict[str, float] | None = None) -> RuleOutcome:
        out = RuleOutcome()
        for trig in self.triggers:
            if isinstance(trig, RiskTrigger):
                trig.position_metrics = position_metrics or {}
            info = trig.has_triggered(ind, has_position, now)
            if not info:
                continue
            for act in trig.actions:
                label = act.name or trig.name
                if act.type == ActionType.SCORE or act.type == ActionType.ENTER:
                    out.bias += act.score * (info.scaling or 1.0)
                    out.notes.append(f"규칙 {label}: {info.reason} ({act.score * (info.scaling or 1.0):+.0f})")
                elif act.type == ActionType.BLOCK_ENTRY and not has_position:
                    out.entry_block = out.entry_block or f"규칙 {label}"
                    out.notes.append(f"규칙 {label}: {info.reason} → 진입 차단")
                elif act.type == ActionType.EXIT and has_position and info.scaling <= 0:
                    out.exit_hint = out.exit_hint or f"규칙 {label}({info.reason})"
        out.bias = max(-40.0, min(40.0, out.bias))
        return out


# ---------------------------------------------------------------------------
# 설정 파일 → RuleSet
# ---------------------------------------------------------------------------
def _action_from_cfg(a: dict) -> Action:
    return Action(ActionType(a.get("type", "score")), float(a.get("score", 0.0)), a.get("name", ""))


def build_trigger(cfg: dict) -> Trigger:
    kind = cfg.get("kind")
    actions = [_action_from_cfg(a) for a in cfg.get("actions", [])]
    name = cfg.get("name", "")
    if kind == "mkt":
        return MktTrigger(cfg["column"], float(cfg["level"]), TriggerDirection(cfg.get("direction", "above")), float(cfg.get("scaling", 1.0)), actions=actions, name=name)
    if kind == "mean_reversion":
        return MeanReversionTrigger(cfg.get("column", "close"), float(cfg.get("z_score_bound", 2.0)), int(cfg.get("rolling_mean_window", 60)), int(cfg.get("rolling_std_window", 60)), actions=actions, name=name)
    if kind == "risk":
        return RiskTrigger(cfg["measure"], float(cfg["level"]), TriggerDirection(cfg.get("direction", "above")), actions=actions, name=name)
    if kind == "time":
        h1, m1 = str(cfg["start"]).split(":")
        h2, m2 = str(cfg["end"]).split(":")
        return TimeWindowTrigger(time(int(h1), int(m1)), time(int(h2), int(m2)), float(cfg.get("scaling", 1.0)), actions=actions, name=name)
    if kind == "all" or kind == "any":
        return AggregateTrigger([build_trigger(t) for t in cfg.get("triggers", [])], mode=kind, actions=actions, name=name)
    if kind == "not":
        return NotTrigger(build_trigger(cfg["trigger"]), actions=actions, name=name)
    raise ValueError(f"알 수 없는 트리거 종류: {kind}")


def build_ruleset(cfgs: list[dict] | None) -> RuleSet:
    return RuleSet([build_trigger(c) for c in (cfgs or [])])


def default_ruleset() -> RuleSet:
    """기본 규칙: VWAP 대비 z-score 평균회귀, 과열 진입 차단, 손실 -1.5R 초과 청산, 장 초반 감점."""
    return RuleSet(
        [
            MeanReversionTrigger("close", 2.2, 60, 60, actions=[Action(ActionType.SCORE, 15.0)], name="z-score 평균회귀"),
            MktTrigger("rsi", 85.0, TriggerDirection.ABOVE, actions=[Action(ActionType.BLOCK_ENTRY)], name="RSI 극단 과열"),
            RiskTrigger("r_multiple", -1.5, TriggerDirection.BELOW, actions=[Action(ActionType.EXIT)], name="손실 R 한도"),
            TimeWindowTrigger(time(9, 0), time(9, 10), scaling=-1.0, actions=[Action(ActionType.SCORE, 10.0)], name="개장 직후 감점"),
        ]
    )
