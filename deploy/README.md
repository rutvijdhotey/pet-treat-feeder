# Deploying to the Raspberry Pi

> Real hardware needs the Pi HAL from milestone M2. Until then the app only runs
> with `--fake`, which the systemd unit deliberately does not pass.

## First-time setup

1. Flash Raspberry Pi OS, enable SSH, set the hostname (e.g. `treatfeeder`), enable
   the GPIO/I2C overlays from [docs/WIRING.md](../docs/WIRING.md) and reboot.
2. From your dev machine: `make deploy-install PI_HOST=treatfeeder`
   (rsyncs the tree to `/opt/pet-treat-feeder` and runs `deploy/install.sh` via sudo).
3. On the Pi, edit `/etc/pet-treat-feeder/config.yaml` (Telegram user id, timezone)
   and `/etc/pet-treat-feeder/.env` (`TELEGRAM_BOT_TOKEN`).
4. `sudo systemctl start pet-treat-feeder`

`install.sh` is idempotent and never overwrites existing config. It does not start the
service while placeholder values remain.

## Updating

```sh
make deploy PI_HOST=treatfeeder            # PI_USER=pi, PI_DIR=/opt/pet-treat-feeder by default
make -n deploy PI_HOST=treatfeeder         # dry run
```

`config.yaml` and `.env` are never synced.

## Where things live

| What | Path |
|---|---|
| Code + venv | `/opt/pet-treat-feeder` |
| Config / secrets | `/etc/pet-treat-feeder/config.yaml`, `.env` (0640 root:feeder) |
| SQLite DB (WAL) | `/var/lib/pet-treat-feeder/` |
| Logs | RAM only (volatile journald): `journalctl -u pet-treat-feeder -f` |

## Rollback

Check out the previous good commit locally and run `make deploy PI_HOST=...` again.
The DB and config are untouched by deploys. To stop the feeder:
`sudo systemctl disable --now pet-treat-feeder`.
