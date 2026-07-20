"""Centralized configuration loading.

Loads `config/config.yaml`, overlays `.env` secrets, and exposes a
typed `Settings` object that the rest of the application uses.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"


def _load_yaml() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_env() -> Dict[str, str]:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    return {k: v for k, v in os.environ.items()}


@dataclass
class Settings:
    """Typed runtime settings, merged from YAML + env."""

    raw: Dict[str, Any] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)

    # convenience accessors
    @property
    def log_level(self) -> str:
        return self.raw.get("project", {}).get("log_level", "INFO")

    @property
    def sqlite_path(self) -> Path:
        p = Path(self.raw.get("project", {}).get("sqlite_path", "data/sniper.sqlite"))
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def exchanges(self) -> Dict[str, Any]:
        return self.raw.get("exchanges", {})

    @property
    def universe(self) -> Dict[str, Any]:
        return self.raw.get("universe", {})

    @property
    def timeframes(self) -> List[str]:
        return self.raw.get("timeframes", ["1m", "5m", "15m", "1h"])

    @property
    def trigger_timeframe(self) -> str:
        return self.raw.get("trigger_timeframe", "1m")

    @property
    def confluence_weights(self) -> Dict[str, float]:
        return self.raw.get("confluence_weights", {})

    @property
    def strategies(self) -> Dict[str, Any]:
        return self.raw.get("strategies", {})

    @property
    def min_signal_score(self) -> float:
        return float(self.raw.get("min_signal_score", 0.55))

    @property
    def risk(self) -> Dict[str, Any]:
        return self.raw.get("risk", {})

    @property
    def backtest(self) -> Dict[str, Any]:
        return self.raw.get("backtest", {})

    @property
    def forward(self) -> Dict[str, Any]:
        return self.raw.get("forward", {})

    @property
    def web(self) -> Dict[str, Any]:
        return self.raw.get("web", {})

    @property
    def telegram(self) -> Dict[str, Any]:
        return self.raw.get("telegram", {})

    @property
    def dex(self) -> Dict[str, Any]:
        return self.raw.get("dex", {})

    @property
    def meme_hunter(self) -> Dict[str, Any]:
        return self.raw.get("meme_hunter", {})

    @property
    def scalping(self) -> Dict[str, Any]:
        return self.raw.get("scalping", {})

    @property
    def assistant(self) -> Dict[str, Any]:
        return self.raw.get("assistant", {})

    @property
    def social(self) -> Dict[str, Any]:
        return self.raw.get("social", {})

    @property
    def fundamentals(self) -> Dict[str, Any]:
        return self.raw.get("fundamentals", {})

    @property
    def llm_api_key(self) -> str:
        return self.env.get("LLM_API_KEY", "")

    @property
    def llm_base_url(self) -> str:
        return self.env.get("LLM_BASE_URL", "https://api.openai.com/v1")

    # secrets from .env -------------------------------------
    @property
    def binance_keys(self) -> Dict[str, str]:
        return {
            "apiKey": self.env.get("BINANCE_API_KEY", ""),
            "secret": self.env.get("BINANCE_API_SECRET", ""),
        }

    @property
    def bybit_keys(self) -> Dict[str, str]:
        return {
            "apiKey": self.env.get("BYBIT_API_KEY", ""),
            "secret": self.env.get("BYBIT_API_SECRET", ""),
        }

    @property
    def telegram_token(self) -> str:
        return self.env.get("TELEGRAM_BOT_TOKEN", "")

    @property
    def telegram_chat_id(self) -> str:
        return self.env.get("TELEGRAM_CHAT_ID", "")

    @property
    def web_auth_token(self) -> str:
        return self.env.get("WEB_AUTH_TOKEN", "")

    # singleton-ish loader ----------------------------------
    @classmethod
    def load(cls) -> "Settings":
        raw = _load_yaml()
        env = _load_env()
        # Merge env secrets into raw for convenience
        if env.get("LLM_API_KEY"):
            raw.setdefault("llm_api_key", env["LLM_API_KEY"])
        if env.get("LLM_BASE_URL"):
            raw.setdefault("llm_base_url", env["LLM_BASE_URL"])
        return cls(raw=raw, env=env)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reload_settings() -> Settings:
    """Force reload (used after editing config files at runtime)."""
    global _settings
    _settings = Settings.load()
    return _settings
