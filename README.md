# auto_data_platform

「xxx の売上指標を取りたい」と入力すると、Claude が BEAM✲ に沿ってヒアリングし、dbt（dim / fct）・dbt Semantic Layer・テストを実装した PR を自動で作る Web アプリとデータ基盤。

- 設計: [docs/design-doc.md](docs/design-doc.md)
- 合格条件: [docs/certificate.md](docs/certificate.md)
- 進捗: [Umbrella issue #18](https://github.com/daikono313/auto_data_platform/issues/18)

構成: FastAPI + React / DuckDB + dbt + MetricFlow / Claude Agent SDK

## セットアップ

前提: [uv](https://docs.astral.sh/uv/)、pnpm 9、Node.js 22、Python 3.12（`uv python install 3.12`）

```
make setup      # 依存のインストール
make lint test  # lint / 型チェック / テスト
make dbt-build  # dbt build と MetricFlow の検証
make dev        # backend (:8000) と frontend (:5173) を起動
```

LLM はローカルでは Claude Code のログイン認証で動きます。API キーで動かす場合は `.env.example` をコピーして `.env` に `ANTHROPIC_API_KEY` を設定してください。

## 使い方

1. `make setup` のあと `make dbt-build`（dbt のメタデータを作る。インタビューのソースカタログが使う）
2. `make dev` で起動し、http://localhost:5173 を開く（`?demo` でダミーデータの画面を見られる）
3. 見たい指標を入力すると、Claude が BEAM✲ に沿って質問する。答えていくと右側の BEAM Canvas と Event Matrix が埋まる
4. 「確定して実装へ」を押すと、生成ジョブが `.adp/worktrees/` で dbt・Semantic Layer・テストを生成して検証し（失敗すれば自己修復）、GitHub に PR を作る

PR は origin の所有者のアカウント（`gh auth token -u <owner>`）で作る。別のアカウントにする場合は `.env` に `ADP_GITHUB_ACCOUNT` を書く。

## 品質の確認

```
make eval                                         # インタビューの eval（実際の LLM、3 シナリオ）
cd backend && uv run python -m adp.evals.e2e --dry-run   # S1 を通しで実行（push と PR 作成はしない）
cd backend && uv run python -m adp.evals.e2e             # S1 を通しで実行し、実際に PR を作る
```

CI では LLM を呼ばず、記録した実際のインタビュー（`backend/tests/fixtures/evals/`）を再生して確認する。

## 安全のための制限

[docs/guardrails.md](docs/guardrails.md) を参照（認証、エージェントの操作範囲、DuckDB の外部アクセス禁止、PR 本文の無害化など）。
