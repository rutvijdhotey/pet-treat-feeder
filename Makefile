.PHONY: install lint format test check

install:
	pip install -e ".[dev]"

lint:
	ruff check .

format:
	ruff format .

test:
	pytest

check: lint test

# ---- Deployment to a Raspberry Pi (see deploy/README.md) ----
PI_HOST ?=
PI_USER ?= pi
PI_DIR ?= /opt/pet-treat-feeder

RSYNC_EXCLUDES = --exclude .git --exclude .venv --exclude __pycache__ \
	--exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
	--exclude .env --exclude config.yaml --exclude tests
RSYNC_FILES = src pyproject.toml README.md LICENSE deploy config.example.yaml .env.example

.PHONY: deploy deploy-install check-pi-host

check-pi-host:
	$(if $(PI_HOST),,$(error PI_HOST is required, e.g. make deploy PI_HOST=treatfeeder))

# rsync the tree; PI_DIR must be writable by PI_USER (first run: use deploy-install)
deploy: check-pi-host
	rsync -az --delete $(RSYNC_EXCLUDES) $(RSYNC_FILES) $(PI_USER)@$(PI_HOST):$(PI_DIR)/
	ssh $(PI_USER)@$(PI_HOST) "sudo $(PI_DIR)/.venv/bin/pip install $(PI_DIR) && sudo systemctl restart pet-treat-feeder && systemctl is-active pet-treat-feeder && journalctl -u pet-treat-feeder -n 20 --no-pager"

deploy-install: check-pi-host
	ssh $(PI_USER)@$(PI_HOST) "sudo install -d -o $(PI_USER) $(PI_DIR)"
	rsync -az --delete $(RSYNC_EXCLUDES) $(RSYNC_FILES) $(PI_USER)@$(PI_HOST):$(PI_DIR)/
	ssh -t $(PI_USER)@$(PI_HOST) "sudo $(PI_DIR)/deploy/install.sh"
