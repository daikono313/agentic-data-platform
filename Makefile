SAMPLE_ORDERS ?= 30000

.PHONY: sample-data setup lint test dbt-build dev dev-backend dev-frontend check-secrets

DBT := cd warehouse && DBT_PROFILES_DIR=. $(abspath .)/backend/.venv/bin/dbt
MF := cd warehouse && DBT_PROFILES_DIR=. $(abspath .)/backend/.venv/bin/mf

setup:
	cd backend && uv sync --locked
	cd frontend && pnpm install --frozen-lockfile
	$(DBT) deps
	$(MAKE) sample-data

sample-data:
	cd warehouse && $(abspath .)/backend/.venv/bin/python scripts/generate_sample_data.py --orders $(SAMPLE_ORDERS)

lint: check-secrets
	cd backend && uv run ruff check . ../warehouse/scripts && uv run ruff format --check . ../warehouse/scripts && uv run mypy src tests
	cd frontend && pnpm lint && pnpm typecheck

test:
	cd backend && uv run pytest
	cd frontend && pnpm test

dbt-build:
	mkdir -p warehouse/data
	$(DBT) deps
	$(DBT) build
	$(DBT) parse
	@if grep -rqsE 'semantic_models?:' warehouse/models; then $(MF) validate-configs; else echo 'mf validate-configs: semantic model が無いためスキップ'; fi
	@if grep -rqsE 'semantic_models?:' warehouse/models; then $(MF) query --metrics revenue --group-by metric_time__month --limit 1; fi

dev-backend:
	cd backend && uv run uvicorn adp.api.app:app --reload --port 8000

dev-frontend:
	cd frontend && pnpm dev

dev:
	$(MAKE) -j2 dev-backend dev-frontend

check-secrets:
	./scripts/check_secrets.sh
