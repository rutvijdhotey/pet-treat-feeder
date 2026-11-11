#!/usr/bin/env bash
# First-time / repeatable setup of the Pet Treat Feeder on a Raspberry Pi.
# Run as root from the deployed tree: sudo /opt/pet-treat-feeder/deploy/install.sh
set -euo pipefail

APP_USER="feeder"
APP_DIR="/opt/pet-treat-feeder"
CONF_DIR="/etc/pet-treat-feeder"
SERVICE="pet-treat-feeder"

usage() {
  cat <<USAGE
Usage: sudo $0 [--help]

Idempotent installer for the Pet Treat Feeder service. It:
  - installs apt dependencies (python3-venv, python3-dev, rsync)
  - creates the '$APP_USER' system user (added to gpio/i2c groups if present)
  - creates $APP_DIR and $CONF_DIR (0750 root:$APP_USER)
  - creates a venv in $APP_DIR and pip-installs the project
  - copies config.example.yaml / .env.example to $CONF_DIR if absent
  - installs the systemd unit and a volatile-journald drop-in, then enables the unit
The service is only started when config/.env no longer hold placeholder values.
USAGE
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
  "") ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root (sudo $0)" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Installing apt dependencies"
apt-get update -qq
apt-get install -y python3-venv python3-dev rsync

echo "==> Creating user $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/pet-treat-feeder --no-create-home \
    --shell /usr/sbin/nologin "$APP_USER"
fi
for grp in gpio i2c; do
  if getent group "$grp" >/dev/null; then
    usermod -aG "$grp" "$APP_USER"
  else
    echo "    note: group '$grp' not found; skipping (enable the GPIO/I2C overlays, see docs/WIRING.md)"
  fi
done

echo "==> Preparing directories"
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  rsync -a --delete \
    --exclude .git --exclude .venv --exclude __pycache__ --exclude '*.db' \
    --exclude .env --exclude config.yaml --exclude tests \
    "$SRC_DIR"/ "$APP_DIR"/
fi
install -d -m 0750 -o root -g "$APP_USER" "$CONF_DIR"

echo "==> Creating virtualenv and installing the project"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install "$APP_DIR"

echo "==> Config files (never overwritten)"
if [ ! -e "$CONF_DIR/config.yaml" ]; then
  install -m 0640 -o root -g "$APP_USER" "$APP_DIR/config.example.yaml" "$CONF_DIR/config.yaml"
  echo "    created $CONF_DIR/config.yaml"
fi
if [ ! -e "$CONF_DIR/.env" ]; then
  install -m 0640 -o root -g "$APP_USER" "$APP_DIR/.env.example" "$CONF_DIR/.env"
  echo "    created $CONF_DIR/.env"
fi

echo "==> Installing systemd unit and journald drop-in"
install -m 0644 "$APP_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 "$APP_DIR/deploy/journald-volatile.conf" /etc/systemd/journald.conf.d/pet-treat-feeder.conf
systemctl restart systemd-journald || echo "    warning: could not restart journald"
systemctl daemon-reload
systemctl enable "$SERVICE"

if grep -q '000000000' "$CONF_DIR/config.yaml" || grep -q 'replace-with-token' "$CONF_DIR/.env"; then
  cat <<MSG

Installed and enabled, but NOT started: placeholder values remain.
Edit these files, then start the service:
  sudo nano $CONF_DIR/config.yaml   # your numeric Telegram user id(s), timezone
  sudo nano $CONF_DIR/.env          # TELEGRAM_BOT_TOKEN from @BotFather
  sudo systemctl start $SERVICE
MSG
else
  systemctl restart "$SERVICE"
  echo "Service started. Logs: journalctl -u $SERVICE -f"
fi
