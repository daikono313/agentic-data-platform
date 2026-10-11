# warehouse（dbt + DuckDB + MetricFlow）

ここは手書きのリファレンスモデルと、アプリが生成するモデルの両方の置き場所。生成コードのお手本になるので、規約を厳密に守る。

**規約の正本は [docs/modeling-conventions.md](../docs/modeling-conventions.md)。** 既存の共通ディメンションは `models/marts/dim_*.sql`。fct はアプリが生成する（生成器は `backend/src/adp/generation/`、手書きのリファレンス fct は生成結果との比較用に `backend/tests/fixtures/generation/reference/` にある）。

- dbt-core 1.12+、dbt-duckdb、dbt-metricflow、dbt_utils。DuckDB ファイルは `data/*.duckdb`（git では追跡しない）。
- レイヤーは `models/staging/stg_*` → `models/intermediate/int_*` → `models/marts/dim_*`, `fct_*`。集計済みマート（`agg_*`）は作らない。集計は Semantic Layer で行う。
- `models/staging/stg_*` はクレンジング層。`cleansing/<schema>.<table>.yml`（と `cleansing/_defaults.yml`）の定義から生成する（`make cleansing`）。stg は手で書かない（ADR 0004）。
- Semantic Layer は **latest spec** で書く。model の YAML の中に `semantic_model:` を置き、columns に `entity` / `dimension`、`metrics` を書く（[参考](https://docs.getdbt.com/docs/build/latest-metrics-spec)）。
- サロゲートキーは `dbt_utils.generate_surrogate_key`。日付ディメンションは `dim_date`（`dbt_utils.date_spine`）。
- SCD は Type1（上書き）のみ。
- テスト: 全 dim / fct の PK に unique と not_null、FK に relationships、fct の粒度に `dbt_utils.unique_combination_of_columns`。BEAM の具体例があれば unit test にする。
- 既存の dim は再利用し、カラムの追加のみ許可する。既存カラムの変更や削除はしない。
- 変更したら `dbt build` と `dbt parse` と `mf validate-configs` を実行する。
- サンプルデータは `scripts/` の生成スクリプトで作る（合成データのみ、乱数シードは固定）。
