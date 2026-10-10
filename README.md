# auto_data_platform

「xxx の売上指標を取りたい」と入力すると、Claude が BEAM✲ に沿ってヒアリングし、dbt（dim / fct）・dbt Semantic Layer・テストを実装した PR を自動で作る Web アプリとデータ基盤。

- 設計: [docs/design-doc.md](docs/design-doc.md)
- 合格条件: [docs/certificate.md](docs/certificate.md)
- 進捗: [Umbrella issue #18](https://github.com/daikono313/auto_data_platform/issues/18)

構成: FastAPI + React / DuckDB + dbt + MetricFlow / Claude Agent SDK
