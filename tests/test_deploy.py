"""Static checks for the deploy/ artefacts (no systemd or Pi required)."""

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
UNIT = DEPLOY / "pet-treat-feeder.service"
INSTALL = DEPLOY / "install.sh"


def _unit() -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]
    cp.read(UNIT)
    return cp


def _make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-n", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )


needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make missing")


def test_unit_keys() -> None:
    s = _unit()["Service"]
    assert s["Restart"] == "always"
    assert s["RestartSec"] == "5"
    assert s["User"] == "feeder"
    assert s["StateDirectory"] == "pet-treat-feeder"
    assert s["WorkingDirectory"] == "/opt/pet-treat-feeder"
    assert s["TimeoutStopSec"] == "20"
    assert "gpio" in s["SupplementaryGroups"]
    assert s["NoNewPrivileges"] == "true"
    assert s["ProtectSystem"] == "strict"
    assert "multi-user.target" in _unit()["Install"]["WantedBy"]
    assert "network-online.target" in _unit()["Unit"]["After"]


def test_unit_execstart() -> None:
    cmd = _unit()["Service"]["ExecStart"]
    assert "-m feeder" in cmd
    assert "--config /etc/pet-treat-feeder/config.yaml" in cmd
    assert "--env /etc/pet-treat-feeder/.env" in cmd
    assert "--fake" not in UNIT.read_text()


def test_unit_has_no_secrets() -> None:
    text = UNIT.read_text().lower()
    assert "token" not in text
    assert "telegram_bot" not in text


def test_install_script_strict_mode() -> None:
    assert "set -euo pipefail" in INSTALL.read_text()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")
def test_install_script_syntax() -> None:
    r = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck missing")
def test_install_script_shellcheck() -> None:
    r = subprocess.run(["shellcheck", str(INSTALL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")
def test_install_script_help() -> None:
    r = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "Usage" in r.stdout


def test_journald_volatile() -> None:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(DEPLOY / "journald-volatile.conf")
    assert cp["Journal"]["Storage"] == "volatile"
    assert cp["Journal"]["RuntimeMaxUse"]


@needs_make
def test_make_deploy_dry_run() -> None:
    r = _make("deploy", "PI_HOST=example.local")
    assert r.returncode == 0, r.stderr
    assert "rsync" in r.stdout
    assert "systemctl restart" in r.stdout
    assert "--exclude .env" in r.stdout
    assert "--exclude config.yaml" in r.stdout
    assert "pi@example.local" in r.stdout


@needs_make
def test_make_deploy_requires_pi_host() -> None:
    r = _make("deploy", "PI_HOST=")
    assert r.returncode != 0
    assert "PI_HOST is required" in r.stderr + r.stdout


@needs_make
def test_make_deploy_install_dry_run() -> None:
    r = _make("deploy-install", "PI_HOST=example.local")
    assert r.returncode == 0, r.stderr
    assert "deploy/install.sh" in r.stdout
