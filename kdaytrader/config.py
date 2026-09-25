"""YAML 설정 로더. ${ENV_VAR} 치환을 지원한다."""
from __future__ import annotations

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
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in REPLACE_KEYS:
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> dict:
    cfg = dict(DEFAULTS)
    if path:
        p = Path(path)
        if p.exists():
            with p.open(encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            cfg = _merge(cfg, user)
    cfg = _expand(cfg)
    if isinstance(cfg.get("watchlist"), list):
        cfg["watchlist"] = {str(c): "" for c in cfg["watchlist"]}
    cfg["watchlist"] = {str(k).zfill(6): (v or "") for k, v in cfg["watchlist"].items()}
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
