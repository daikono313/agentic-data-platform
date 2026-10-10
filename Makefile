SAMPLE_ORDERS ?= 30000

.PHONY: sample-data setup lint test dbt-build charts eval dev dev-backend dev-frontend check-secrets

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
	@if grep -rqsE '^ *metrics:' warehouse/models; then $(MF) validate-configs; else echo 'mf validate-configs: metric が無いためスキップ'; fi
	@if grep -rqsE '^ *metrics:' warehouse/models; then $(MF) list metrics; fi
	cd backend && uv run python -m adp.charts check

# チャートの元ファイル（warehouse/charts/metrics/）から dbt Charts のボードを書き出す
charts:
	cd backend && uv run python -m adp.charts compile

# 実際の LLM（既定は Claude Code のログイン認証）でインタビューの eval を回す
eval:
	cd backend && uv run python -m adp.evals interview $(if $(SCENARIO),--scenario $(SCENARIO))

dev-backend:
	cd backend && uv run uvicorn --factory adp.api.app:build_default_app --reload --port 8000

dev-frontend:
	cd frontend && pnpm dev

dev:
	$(MAKE) -j2 dev-backend dev-frontend

check-secrets:
	./scripts/check_secrets.sh
