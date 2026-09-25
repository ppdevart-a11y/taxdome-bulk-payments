COMPOSE := docker compose
PSQL := $(COMPOSE) exec -T postgres psql -U postgres -d bulk_payments -v ON_ERROR_STOP=1

.PHONY: help install up down seed balances sample test lint format demo

help: ## List targets
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install dependencies into .venv
	uv sync

up: ## Build and start Postgres, migrations, 2 app replicas and nginx on :8080
	$(COMPOSE) up --build --detach --wait

down: ## Stop everything and delete the database volume
	$(COMPOSE) down --volumes

seed: ## Reset the database to the three sample firms
	$(PSQL) < scripts/seed.sql

balances: ## Show firm balances in dollars
	$(PSQL) -c "SELECT id, name, to_char(balance_cents / 100.0, 'FM999,999,990.00') AS balance_usd FROM firms ORDER BY id"

sample: ## POST the sample request through the load balancer
	curl -sS -i -X POST localhost:8080/bulk_payments \
		-H 'Content-Type: application/json' -d @scripts/sample_request.json; echo

test: ## Unit + integration tests (starts a throwaway Postgres via testcontainers)
	uv run pytest

lint: ## Lint, format check and type check
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format: ## Auto-format and fix lint
	uv run ruff format .
	uv run ruff check --fix .

demo: ## Concurrent requests through nginx to both replicas, then check invariants
	uv run python scripts/race_demo.py
