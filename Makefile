SAMPLE_ORDERS ?= 30000

.PHONY: sample-data setup lint test dbt-build charts ontology cleansing eval-repair eval e2e eval-compare feedback-report feedback-drafts dev dev-backend dev-frontend check-secrets

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
	cd backend && uv run python -m adp.ontology check
	cd backend && uv run python -m adp.cleansing check

# クレンジングの定義（warehouse/cleansing/）からクレンジング層（models/staging/stg_*）を生成する。
# 雛形は `cd backend && uv run python -m adp.cleansing draft raw.<table>`（源泉のプロファイル画面にも出る）
cleansing:
	cd backend && uv run python -m adp.cleansing build
	cd backend && uv run python -m adp.cleansing check

# オントロジーの正本（ontology/ontology.yml）から docs/ontology.md（Mermaid の図）を生成し、源泉・dbt と照合する
ontology:
	cd backend && uv run python -m adp.ontology build
	cd backend && uv run python -m adp.ontology check

# チャートの元ファイル（warehouse/chart_specs/）から dbt Charts のボードを書き出す
charts:
	cd backend && uv run python -m adp.charts compile

# 実際の LLM（既定は Claude Code のログイン認証）でインタビューの eval を回す
eval:
	cd backend && uv run python -m adp.evals interview $(if $(SCENARIO),--scenario $(SCENARIO)) $(if $(REPEAT),--repeat $(REPEAT))

# 実際の LLM で通しの E2E（S1。V2=1 で S3・S4、V3=1 で S5。V3 の既定のシナリオは reviews）。既定は dry-run（push・PR 作成をしない）。PR=1 で本物の PR
e2e:
	cd backend && uv run python -m adp.evals.e2e --scenario $(or $(SCENARIO),$(if $(V3),reviews,sales_by_store)) $(if $(V2),--v2) $(if $(V3),--v3) $(if $(PR),,--dry-run)

# 生成の自己修復の eval（#78）: 失敗を入れた生成物を実際の LLM で直せるか。make eval-repair [SCENARIO=名前] [REPEAT=N]
eval-repair:
	cd backend && uv run python -m adp.evals repair $(if $(SCENARIO),--scenario $(SCENARIO)) $(if $(REPEAT),--repeat $(REPEAT))

# eval の前後比較（プロンプトを直したら PR に貼る。certificate C10）: make eval-compare BEFORE=.adp/evals/<前> AFTER=.adp/evals/<後>
eval-compare:
	cd backend && uv run python -m adp.evals compare $(abspath $(BEFORE)) $(abspath $(AFTER))

# フィードバックの集計（SUMMARY=1 で Claude による要約と改善案）。会話の抜粋を含むので公開しない
feedback-report:
	cd backend && uv run python -m adp.evals feedback-report $(if $(SUMMARY),--summary)

# 低評価のセッションから eval シナリオの下書きを .adp/eval-drafts/ に作る（合成に書き換えてから evals/ へ）
feedback-drafts:
	cd backend && uv run python -m adp.evals feedback-drafts

dev-backend:
	cd backend && uv run uvicorn --factory adp.api.app:build_default_app --reload --port 8000

dev-frontend:
	cd frontend && pnpm dev

dev:
	$(MAKE) -j2 dev-backend dev-frontend

check-secrets:
	./scripts/check_secrets.sh
