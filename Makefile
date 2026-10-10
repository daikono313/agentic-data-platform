.PHONY: setup lint test dbt-build dev dev-backend dev-frontend check-secrets

UV := uv run --project backend
DBT := cd warehouse && DBT_PROFILES_DIR=. $(abspath .)/backend/.venv/bin/dbt
MF := cd warehouse && DBT_PROFILES_DIR=. $(abspath .)/backend/.venv/bin/mf

setup:
	cd backend && uv sync
	cd frontend && pnpm install
	$(DBT) deps

lint: check-secrets
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd frontend && pnpm lint && pnpm typecheck

test:
	cd backend && uv run pytest
	cd frontend && pnpm test

dbt-build:
	$(DBT) deps
	$(DBT) build
	$(DBT) parse
	@if grep -rqs 'semantic_model:' warehouse/models; then $(MF) validate-configs; else echo 'mf validate-configs: semantic model が無いためスキップ'; fi

dev-backend:
	cd backend && uv run uvicorn adp.api.app:app --reload --port 8000

dev-frontend:
	cd frontend && pnpm dev

dev:
	$(MAKE) -j2 dev-backend dev-frontend

check-secrets:
	./scripts/check_secrets.sh
