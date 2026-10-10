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
