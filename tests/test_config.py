from __future__ import annotations

import copy
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from feeder.config import Config, ConfigError, load_config

TOKEN = "123456:SECRET-token-value"
ROOT = Path(__file__).resolve().parent.parent
BASE: dict = {
    "telegram": {"allowed_user_ids": [42]},
    "timezone": "America/Los_Angeles",
    "treats": {"daily_cap": 8, "capacity": 80},
    "supply": {},
    "schedule": {},
    "notifications": {"daily_summary": "21:00"},
    "leds": {"brightness": 0.4},
}


def write(tmp_path: Path, data: dict | None = None) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(BASE if data is None else data))
    return p


def mod(**sections: object) -> dict:
    d = copy.deepcopy(BASE)
    for k, v in sections.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k].update(v)
        else:
            d[k] = v
    return d


def load(tmp_path: Path, data: dict | None = None, **kw) -> Config:
    return load_config(
        write(tmp_path, data), env_path=None, environ={"TELEGRAM_BOT_TOKEN": TOKEN}, **kw
    )


def test_example_file_loads_unchanged() -> None:
    cfg = load_config(
        ROOT / "config.example.yaml", env_path=None, environ={"TELEGRAM_BOT_TOKEN": TOKEN}
    )
    assert cfg.telegram.allowed_user_ids == [0]
    assert cfg.treats.daily_cap == 8
    assert cfg.supply.empty_mm is None
    assert cfg.notifications.daily_summary == time(21, 0)
    assert cfg.tz == ZoneInfo("America/Los_Angeles")


def test_defaults(tmp_path: Path) -> None:
    cfg = load(tmp_path, {"telegram": {"allowed_user_ids": [1]}, "timezone": "UTC"})
    assert cfg.treats.daily_cap == 8 and cfg.treats.capacity == 80
    assert cfg.supply.low_threshold_pct == 20 and cfg.supply.low_clear_pct == 30
    assert cfg.supply.refill_rise_pct == 15 and cfg.supply.poll_interval_s == 300
    assert cfg.schedule.default_jitter_min == 0
    assert cfg.leds.brightness == 0.4
    assert cfg.pins == {}
    assert cfg.db_path == "/var/lib/pet-treat-feeder/feeder.db"


def test_token_property_and_repr(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert cfg.telegram_token == TOKEN
    assert TOKEN not in repr(cfg)
    assert TOKEN not in str(cfg)


def test_token_from_env_file_not_mutating_environ(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=from-dotenv\n")
    import os

    cfg = load_config(write(tmp_path), env_path=env, environ={})
    assert cfg.telegram_token == "from-dotenv"
    assert "TELEGRAM_BOT_TOKEN" not in os.environ


def test_process_env_wins_over_dotenv(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=from-dotenv\n")
    cfg = load_config(write(tmp_path), env_path=env, environ={"TELEGRAM_BOT_TOKEN": "proc"})
    assert cfg.telegram_token == "proc"


def test_default_environ_is_os_environ(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "osenv")
    assert load_config(write(tmp_path), env_path=None).telegram_token == "osenv"


@pytest.mark.parametrize("environ", [{}, {"TELEGRAM_BOT_TOKEN": ""}, {"TELEGRAM_BOT_TOKEN": "  "}])
def test_missing_or_blank_token(tmp_path: Path, environ) -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(write(tmp_path), env_path=None, environ=environ)


def test_missing_env_file_is_fine_if_token_in_environ(tmp_path: Path) -> None:
    cfg = load_config(
        write(tmp_path), env_path=tmp_path / "nope.env", environ={"TELEGRAM_BOT_TOKEN": "x"}
    )
    assert cfg.telegram_token == "x"


def test_missing_config_file_hint(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"config\.example\.yaml"):
        load_config(tmp_path / "config.yaml", env_path=None, environ={"TELEGRAM_BOT_TOKEN": "x"})


def test_config_error_is_value_error() -> None:
    assert issubclass(ConfigError, ValueError)


def test_invalid_yaml_and_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("a: [unclosed")
    with pytest.raises(ConfigError):
        load_config(p, env_path=None, environ={"TELEGRAM_BOT_TOKEN": "x"})
    p.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError):
        load_config(p, env_path=None, environ={"TELEGRAM_BOT_TOKEN": "x"})


def test_invalid_timezone(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="timezone"):
        load(tmp_path, mod(timezone="Mars/Olympus"))


@pytest.mark.parametrize("bad", ["25:00", "9pm", "21:60", "", "21"])
def test_bad_daily_summary(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="daily_summary"):
        load(tmp_path, mod(notifications={"daily_summary": bad}))


def test_daily_summary_parsed(tmp_path: Path) -> None:
    cfg = load(tmp_path, mod(notifications={"daily_summary": "07:05"}))
    assert cfg.notifications.daily_summary == time(7, 5)


@pytest.mark.parametrize(
    "section",
    [
        {"supply": {"low_threshold_pct": 30, "low_clear_pct": 30}},
        {"supply": {"low_threshold_pct": 40, "low_clear_pct": 30}},
        {"supply": {"empty_mm": 100}},
        {"supply": {"full_mm": 20}},
        {"supply": {"empty_mm": 20, "full_mm": 100}},
        {"supply": {"empty_mm": 50, "full_mm": 50}},
        {"supply": {"low_threshold_pct": -1}},
        {"supply": {"low_clear_pct": 101}},
        {"telegram": {"allowed_user_ids": []}},
        {"telegram": {"allowed_user_ids": ["abc"]}},
        {"treats": {"daily_cap": 0}},
        {"treats": {"capacity": 0}},
        {"leds": {"brightness": 1.5}},
        {"leds": {"brightness": -0.1}},
        {"surprise": 1},
        {"treats": {"bogus": 1}},
    ],
)
def test_invalid_values_rejected(tmp_path: Path, section: dict) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, mod(**section))


def test_valid_calibration(tmp_path: Path) -> None:
    cfg = load(tmp_path, mod(supply={"empty_mm": 120, "full_mm": 20}))
    assert (cfg.supply.empty_mm, cfg.supply.full_mm) == (120, 20)


def test_error_message_readable_and_leaks_no_token(tmp_path: Path) -> None:
    data = mod(treats={"daily_cap": 0}, leds={"brightness": 9})
    with pytest.raises(ConfigError) as ei:
        load(tmp_path, data)
    msg = str(ei.value)
    assert "treats.daily_cap" in msg and "leds.brightness" in msg
    assert "\n" in msg
    assert TOKEN not in msg


def test_token_in_yaml_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, mod(telegram_token="leak"))
