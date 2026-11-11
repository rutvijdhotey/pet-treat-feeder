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
