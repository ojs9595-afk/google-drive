"""YAML 설정 로더. ${ENV_VAR} 치환을 지원한다."""
from __future__ import annotations

import copy
import logging
import os
import re
from dataclasses import fields
from datetime import time
from pathlib import Path
from typing import Any

import yaml

from .risk import RiskParams
from .strategy.ensemble import StrategyParams
from .strategy.quant import QuantParams
from .strategy.rules import RuleSet, build_ruleset, default_ruleset

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")
log = logging.getLogger(__name__)


class _Loader(yaml.SafeLoader):
    """앞자리가 0인 숫자(종목코드 000660, 005930)를 8진수로 바꾸지 않고 문자열 그대로 읽는 로더."""


_LEADING_ZERO_RE = re.compile(r"^[-+]?0[0-9_]+$")


def _construct_int(loader, node):
    value = str(loader.construct_scalar(node)).replace("_", "")
    if _LEADING_ZERO_RE.match(value) and not value.startswith(("0x", "0o", "0b", "-", "+")):
        return value  # 종목코드 등: 문자열 유지
    return yaml.SafeLoader.construct_yaml_int(loader, node)


_Loader.add_constructor("tag:yaml.org,2002:int", _construct_int)
_INT_RE = re.compile(r"^(?:[-+]?0b[0-1_]+|[-+]?0x[0-9a-fA-F_]+|[-+]?0o[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0[0-9_]+)$")
_Loader.yaml_implicit_resolvers = {k: [(t, r) for (t, r) in v if t != "tag:yaml.org,2002:int"] for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
for ch in "-+0123456789":
    _Loader.yaml_implicit_resolvers.setdefault(ch, []).append(("tag:yaml.org,2002:int", _INT_RE))


class _Quoted(str):
    """항상 따옴표로 기록되는 문자열 (종목코드 키)."""


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Quoted, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", str(v), style="'"))


def yaml_dump(data) -> str:
    def q(obj):
        if isinstance(obj, dict):
            return {(_Quoted(k) if isinstance(k, str) and k.isdigit() else k): q(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [q(x) for x in obj]
        if isinstance(obj, str) and obj.isdigit() and obj.startswith("0"):
            return _Quoted(obj)
        return obj

    return yaml.dump(q(data), Dumper=_Dumper, allow_unicode=True, sort_keys=False)


def yaml_load(text: str):
    return yaml.load(text, Loader=_Loader)

DEFAULT_WATCHLIST = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "005380": "현대차",
    "000270": "기아",
    "035420": "NAVER",
    "035720": "카카오",
    "068270": "셀트리온",
    "373220": "LG에너지솔루션",
    "006400": "삼성SDI",
    "105560": "KB금융",
}

DEFAULTS: dict[str, Any] = {
    "mode": "paper",  # paper | live
    "feed": "sim",  # sim | naver | kis
    "interval_min": 1,
    "initial_cash": 10_000_000,
    "watchlist": DEFAULT_WATCHLIST,
    "auto_trade": True,
    "screener": {"enabled": False, "source": "naver", "limit": 15, "min_price": 2000, "max_price": 500000, "refresh_min": 10, "max_universe": 25},
    "kis": {"app_key": "${KIS_APP_KEY:-}", "app_secret": "${KIS_APP_SECRET:-}", "account": "${KIS_ACCOUNT:-}", "paper": True},
    "naver": {"poll_interval": 1.5},
    "sim": {"speed": 60, "history_days": 3, "seed": 42},
    "telegram": {"token": "${TELEGRAM_BOT_TOKEN:-}", "chat_id": "${TELEGRAM_CHAT_ID:-}"},
    "strategy": {},
    "risk": {},
    "quant": {"enabled": True, "pairs": {}},
    "rules": {"enabled": True, "use_defaults": True, "triggers": []},
    "web": {"enabled": True, "host": "127.0.0.1", "port": 8787},
    "context": {"enabled": True},
    "log_dir": "logs",
}


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        def rep(m):
            return os.environ.get(m.group(1), m.group(2) or "")

        return _ENV_RE.sub(rep, v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


REPLACE_KEYS = {"watchlist"}  # 사용자가 지정하면 기본값과 합치지 않고 통째로 대체


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)  # 기본값(DEFAULTS)이 오염되지 않도록 깊은 복사
    for k, v in (override or {}).items():
        if v is None:
            continue  # 비어 있는 섹션은 기본값 유지
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in REPLACE_KEYS:
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _normalize_watchlist(wl) -> dict[str, str]:
    if wl is None:
        return {}
    if isinstance(wl, list):
        wl = {c: "" for c in wl}
    out = {}
    for k, v in dict(wl).items():
        code = str(k).strip()
        if not code:
            continue
        if not code.isdigit():
            raise ValueError(f"종목코드는 숫자 6자리여야 합니다 (입력값: {code!r})")
        out[code.zfill(6)] = str(v or "")
    return out


def validate_config(cfg: dict) -> None:
    """저장/실행 전 공통 검증. 문제가 있으면 ValueError."""
    try:
        im = int(cfg.get("interval_min", 1))
    except (TypeError, ValueError):
        raise ValueError("interval_min: 정수여야 합니다")
    if im < 1:
        raise ValueError("interval_min: 1 이상의 정수여야 합니다")
    try:
        cash = float(cfg.get("initial_cash", 0))
    except (TypeError, ValueError):
        raise ValueError("initial_cash: 숫자여야 합니다")
    if cash <= 0:
        raise ValueError("initial_cash: 0 보다 커야 합니다")
    sp = float((cfg.get("sim") or {}).get("speed", 60) or 0)
    if sp < 0:
        raise ValueError("sim.speed: 0 이상이어야 합니다")
    build_strategy_params(cfg)
    build_risk_params(cfg)
    build_quant_params(cfg)
    build_pairs(cfg)
    build_rules(cfg)


def load_config(path: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    config_error = ""
    if path:
        p = Path(path)
        if p.exists():
            try:
                user = yaml_load(p.read_text(encoding="utf-8")) or {}
                if not isinstance(user, dict):
                    raise ValueError("최상위가 매핑(key: value)이 아닙니다")
            except Exception as e:  # YAML 문법 오류 등: 기본값으로 계속 실행하고 화면에 알린다
                config_error = f"{p.name} 읽기 실패 ({type(e).__name__}): {str(e)[:200]}"
                log.warning(config_error)
                user = {}
            cfg = _merge(cfg, user)
    cfg = _expand(cfg)
    cfg["watchlist"] = _normalize_watchlist(cfg.get("watchlist"))
    cfg["log_dir"] = cfg.get("log_dir") or "logs"
    for k, v in DEFAULTS.items():
        if isinstance(v, dict) and not isinstance(cfg.get(k), dict):
            cfg[k] = copy.deepcopy(v)
    if config_error:
        cfg["_config_error"] = config_error
    return cfg


TIME_FIELDS = {"entry_start", "entry_end", "force_close"}


def _parse_time(v: Any, key: str = "") -> Any:
    if isinstance(v, time):
        return v
    if isinstance(v, str):
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", v)
        if m and 0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60:
            return time(int(m.group(1)), int(m.group(2)))
        if key in TIME_FIELDS:
            raise ValueError(f"{key}: 시각 형식은 HH:MM 이어야 합니다 (입력값: {v!r})")
    elif key in TIME_FIELDS:
        raise ValueError(f"{key}: 시각 형식은 HH:MM 이어야 합니다 (입력값: {v!r})")
    return v


def build_strategy_params(cfg: dict) -> StrategyParams:
    allowed = {f.name for f in fields(StrategyParams)}
    kwargs = {}
    for k, v in (cfg.get("strategy") or {}).items():
        if k in allowed:
            kwargs[k] = tuple(v) if isinstance(v, list) else v
    return StrategyParams(**kwargs)


def build_risk_params(cfg: dict) -> RiskParams:
    allowed = {f.name for f in fields(RiskParams)}
    kwargs = {k: _parse_time(v, k) for k, v in (cfg.get("risk") or {}).items() if k in allowed}
    for k in ("risk_per_trade", "max_position_pct", "daily_loss_limit_pct"):
        if k in kwargs and not (0 < float(kwargs[k]) <= 1):
            raise ValueError(f"{k}: 0 초과 1 이하의 비율이어야 합니다 (입력값: {kwargs[k]})")
    return RiskParams(**kwargs)


def build_quant_params(cfg: dict) -> QuantParams:
    allowed = {f.name for f in fields(QuantParams)}
    kwargs = {}
    for k, v in (cfg.get("quant") or {}).items():
        if k in allowed:
            kwargs[k] = tuple(v) if isinstance(v, list) else v
    return QuantParams(**kwargs)


def build_pairs(cfg: dict) -> dict[str, str]:
    pairs = (cfg.get("quant") or {}).get("pairs") or {}
    out = {}
    for a, b in pairs.items():
        a, b = str(a).zfill(6), str(b).zfill(6)
        out[a] = b
        out.setdefault(b, a)
    return out


def build_rules(cfg: dict) -> RuleSet | None:
    r = cfg.get("rules") or {}
    if not r.get("enabled", True):
        return None
    rs = default_ruleset() if r.get("use_defaults", True) else RuleSet()
    rs.triggers.extend(build_ruleset(r.get("triggers")).triggers)
    return rs
