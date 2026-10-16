.RECIPEPREFIX = >

.PHONY: install lint format test test-integration check check-all run db-up db-down db-migrate db-revision

install:
> uv sync

lint:
> uv run ruff check .
> uv run ruff format --check .

format:
> uv run ruff format .
> uv run ruff check --fix .

# Unit and API tests; no database needed.
test:
> uv run pytest -m "not integration"

# Needs Postgres (make db-up) and TEST_DATABASE_URL; fails, not skips, without them.
test-integration:
> uv run pytest -m integration

check: lint test

check-all: check test-integration

run:
> uv run uvicorn transaction_analytics.main:app --reload

db-up:
> docker compose up -d --wait db

db-down:
> docker compose down

# Applies migrations to DATABASE_URL (the dev database).
db-migrate:
> uv run alembic upgrade head

# Usage: make db-revision m="add something". Review the file; never trust autogenerate blindly.
db-revision:
> @test -n "$(m)" || { echo 'usage: make db-revision m="message"'; exit 1; }
> uv run alembic revision -m "$(m)"
