"""Chronos configuration loader.

Resolution order (highest precedence wins):
  CLI flag  >  env var  >  config.yml  >  built-in default

Discovery order for config.yml:
  1. $CHRONOS_CONFIG
  2. ./config.yml          (repo-local / dev)
  3. ~/.chronos/config.yml (user-level)
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_DEFAULTS: dict[str, Any] = {
    "network": {"http_port": 7070, "mcp_port": 7071, "host": "127.0.0.1"},
    "storage": {"home": "~/.chronos", "db_path": None},
    "logging": {"level": "critical", "show_uvicorn": False},
    "sync": {
        "poll_interval_seconds": 30,
        "incremental_interval_seconds": 60,
        "gmail": {"concurrence": 10, "page_size": 500, "timeout_seconds": 30},
        "calendar": {
            "page_size": 250,
            "timeout_seconds": 30,
            "lookback_days": 30,
            "lookahead_days": 365,
        },
    },
    "daemon": {
        "sigint_wipes_data": True,
        "show_welcome_on_first_run": True,
        "progress_refresh_hz": 4,
    },
    "rate_limit": {"base_seconds": 2.0, "max_seconds": 64.0, "jitter_pct": 20},
}


def _discover_config_path() -> Path | None:
    explicit = os.environ.get("CHRONOS_CONFIG")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    cwd_config = Path("config.yml")
    if cwd_config.exists():
        return cwd_config
    home_config = Path(os.environ.get("CHRONOS_HOME", "~/.chronos")).expanduser() / "config.yml"
    if home_config.exists():
        return home_config
    return None


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Return the merged config dict. Cached for the lifetime of the process."""
    config = dict(_DEFAULTS)
    path = _discover_config_path()
    if path is None:
        return config
    try:
        with path.open("r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return config
    settings = user.get("settings") if isinstance(user, dict) else None
    if isinstance(settings, dict):
        config = _deep_merge(config, settings)
    return config


def get(path: str, default: Any = None) -> Any:
    """Read a dotted-path setting from config (e.g. 'sync.gmail.concurrence')."""
    node: Any = load_config()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def reset_cache() -> None:
    """Force re-read on the next load_config() call. Used in tests."""
    load_config.cache_clear()
