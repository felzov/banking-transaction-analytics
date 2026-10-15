.RECIPEPREFIX = >

.PHONY: install lint format test check run db-up db-down

install:
> uv sync

lint:
> uv run ruff check .
> uv run ruff format --check .

format:
> uv run ruff format .
> uv run ruff check --fix .

test:
> uv run pytest

check: lint test

run:
> uv run uvicorn transaction_analytics.main:app --reload

db-up:
> docker compose up -d db

db-down:
> docker compose down
