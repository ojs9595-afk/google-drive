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
    "screener": {"enabled": False, "source": "naver", "limit": 15, "min_price": 2000, "max_price": 500000},
    "kis": {"app_key": "${KIS_APP_KEY:-}", "app_secret": "${KIS_APP_SECRET:-}", "account": "${KIS_ACCOUNT:-}", "paper": True},
    "naver": {"poll_interval": 1.5},
    "sim": {"speed": 60, "history_days": 3, "seed": 42},
    "telegram": {"token": "${TELEGRAM_BOT_TOKEN:-}", "chat_id": "${TELEGRAM_CHAT_ID:-}"},
    "strategy": {},
    "risk": {},
    "quant": {"enabled": True, "pairs": {}},
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


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
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


def _parse_time(v: Any) -> Any:
    if isinstance(v, str) and re.match(r"^\d{1,2}:\d{2}$", v):
        h, m = v.split(":")
        return time(int(h), int(m))
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
    kwargs = {k: _parse_time(v) for k, v in (cfg.get("risk") or {}).items() if k in allowed}
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
