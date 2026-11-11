"""Configuration loading and validation (PRD 3.10)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import time
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

Pct = Annotated[float, Field(ge=0, le=100)]


class ConfigError(ValueError):
    """Raised for any problem loading or validating configuration."""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TelegramConfig(_Base):
    allowed_user_ids: list[int] = Field(min_length=1)


class TreatsConfig(_Base):
    daily_cap: int = Field(default=8, ge=1)
    capacity: int = Field(default=80, ge=1)


class SupplyConfig(_Base):
    low_threshold_pct: Pct = 20
    low_clear_pct: Pct = 30
    refill_rise_pct: Pct = 15
    poll_interval_s: int = Field(default=300, ge=1)
    empty_mm: int | None = None
    full_mm: int | None = None

    @model_validator(mode="after")
    def _check(self) -> SupplyConfig:
        if self.low_clear_pct <= self.low_threshold_pct:
            raise ValueError("low_clear_pct must be greater than low_threshold_pct")
        if (self.empty_mm is None) != (self.full_mm is None):
            raise ValueError("empty_mm and full_mm must both be set or both be null")
        if self.empty_mm is not None and self.full_mm is not None and self.empty_mm <= self.full_mm:
            raise ValueError("empty_mm must be greater than full_mm (sensor looks down)")
        return self


class ScheduleConfig(_Base):
    default_jitter_min: int = Field(default=0, ge=0)


class NotificationsConfig(_Base):
    daily_summary: time = time(21, 0)

    @field_validator("daily_summary", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> Any:
        if isinstance(v, str):
            parts = v.strip().split(":")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                raise ValueError("must be a time of day formatted HH:MM")
            try:
                return time(int(parts[0]), int(parts[1]))
            except ValueError as exc:
                raise ValueError("must be a valid time of day formatted HH:MM") from exc
        return v


class LedsConfig(_Base):
    brightness: float = Field(default=0.4, ge=0.0, le=1.0)


class Config(_Base):
    telegram: TelegramConfig
    timezone: str
    treats: TreatsConfig = TreatsConfig()
    supply: SupplyConfig = SupplyConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    leds: LedsConfig = LedsConfig()
    pins: dict[str, int] = {}
    db_path: str = "/var/lib/pet-treat-feeder/feeder.db"
    telegram_token_secret: SecretStr = Field(repr=False, exclude=True)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"unknown IANA time zone {v!r}") from exc
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def telegram_token(self) -> str:
        return self.telegram_token_secret.get_secret_value()


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in err["loc"] if p != "telegram_token_secret") or "(root)"
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"  - {loc}: {msg}")
    return "Invalid configuration:\n" + "\n".join(lines)


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path | None = ".env",
    environ: Mapping[str, str] | None = None,
) -> Config:
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.yaml to {path.name} and edit it."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    env = os.environ if environ is None else environ
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token and env_path is not None and Path(env_path).is_file():
        token = (dotenv_values(env_path).get(TOKEN_ENV) or "").strip()
    if not token:
        raise ConfigError(
            f"{TOKEN_ENV} is not set. Put it in the environment or in {env_path or '.env'} "
            "(see .env.example)."
        )

    if "telegram_token" in raw or "telegram_token_secret" in raw:
        raise ConfigError(f"Do not put the Telegram token in {path.name}; use {TOKEN_ENV}")
    try:
        return Config.model_validate({**raw, "telegram_token_secret": token})
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from None
