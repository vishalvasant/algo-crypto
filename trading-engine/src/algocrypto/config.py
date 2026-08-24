from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_yaml(name: str) -> dict[str, Any]:
    config_dir = Path(os.environ.get("CONFIG_DIR", "config"))
    path = config_dir / name
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data or {}


def _resolve_env_file() -> str | None:
    candidates = [
        Path(os.environ.get("ALGOCRYPTO_ENV_FILE", "")),
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[3] / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for path in candidates:
        if str(path) and path.is_file():
            return str(path)
    return None


_env_file = _resolve_env_file()


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    trading_mode: str = Field(default="paper", alias="TRADING_MODE")
    database_url: str = Field(
        default="postgresql://algocrypto:algocrypto@localhost:5433/algocrypto",
        alias="DATABASE_URL",
    )
    config_dir: str = Field(default="./config", alias="CONFIG_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    delta_api_key: str | None = Field(default=None, alias="DELTA_API_KEY")
    delta_api_secret: str | None = Field(default=None, alias="DELTA_API_SECRET")
    delta_env: str = Field(default="india", alias="DELTA_ENV")


@lru_cache
def get_env() -> EnvSettings:
    return EnvSettings()


class AppConfig:
    def __init__(self) -> None:
        self.env = get_env()
        self.broker = _load_yaml("broker_config.yaml")
        self.symbols = _load_yaml("symbols_config.yaml")
        self.strategy = _load_yaml("strategy_config.yaml")
        self.validator = _load_yaml("validator_config.yaml")
        self.risk = _load_yaml("risk_config.yaml")
        self.execution = _load_yaml("execution_config.yaml")
        self.position_exit = _load_yaml("position_exit_config.yaml")
        self.paper_trading = _load_yaml("paper_trading_config.yaml")
        self.market_session = _load_yaml("market_session_config.yaml")
        self.runtime = _load_yaml("runtime_config.yaml")
        self.logging = _load_yaml("logging_config.yaml")
        self.ml = _load_yaml("ml_config.yaml")
        self.data_availability = _load_yaml("data_availability_config.yaml")
        try:
            self.fees = _load_yaml("fees_config.yaml")
        except FileNotFoundError:
            self.fees = {}

    @property
    def is_paper(self) -> bool:
        return self.env.trading_mode.lower() == "paper"

    @property
    def is_live(self) -> bool:
        return self.env.trading_mode.lower() == "live"


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
