# warehouse（dbt + DuckDB + MetricFlow）

dbt プロジェクト。**ここのコード（SQL・YAML）は AI か決定的なプログラムが書く。人間が直すのはクレンジングの定義（`cleansing/`）だけ**（[design-doc の「ファイルの持ち主」](../docs/design-doc.md#ファイルの持ち主)）。

| 場所 | 誰が書くか |
|---|---|
| `cleansing/*.yml` | プロファイルから雛形 → **人間**が確認・修正（手で直してよい） |
| `models/staging/stg_*` | `cleansing/` から決定的に生成（`make cleansing`） |
| 生成した `fct_*` と Semantic Layer・属性の履歴（`snapshots/snp_*.yml`・`models/marts/dim_*_history`）・テスト（`tests/assert_<fct>_reconciles_with_source.sql` を含む） | アプリの生成器（テンプレート）と生成エージェント（AI。テンプレートで作れない作業と修復） |
| 共通ディメンション `models/marts/dim_*`（生成した `dim_*_history` を除く）・`_marts.yml` | 開発時に Claude Code が書いたもの（生成器の元ではない） |
| `models/staging/_sources.yml` | Claude Code |
| `models/staging/_<source>__sources.yml` | 取り込み（#69）と生成が書く（宣言の無い源泉の宣言） |
| `chart_specs/` | 生成ジョブが書く（`store_sales.yml` は開発時の見本）。エージェントは書けない |
| `charts/` | `chart_specs/` から決定的に書き出す（`make charts`）。エージェントは書けない |
| `macros/`・`dbt_project.yml`・`profiles.yml`・`scripts/` | Claude Code（アプリの開発） |

生成コードのお手本になるので、規約を厳密に守る。

**規約の正本は [docs/modeling-conventions.md](../docs/modeling-conventions.md)。** 既存の共通ディメンションは `models/marts/dim_*.sql`。fct はアプリが生成する（生成器は `backend/src/adp/generation/`、リファレンス fct（生成器とは別に書いたもの）は生成結果との比較用に `backend/tests/fixtures/generation/reference/` にある）。規約に合っているかは `make dbt-build` の最後に `adp.generation.conventions` が検査する。

- dbt-core 1.12+、dbt-duckdb、dbt-metricflow、dbt_utils。DuckDB ファイルは `data/*.duckdb`（git では追跡しない）。
- レイヤーは `models/staging/stg_*` → `models/intermediate/int_*` → `models/marts/dim_*`, `fct_*`。集計済みマート（`agg_*`）は作らない。集計は Semantic Layer で行う。
- `models/staging/stg_*` はクレンジング層。`cleansing/<schema>.<table>.yml`（と `cleansing/_defaults.yml`）の定義から生成する（`make cleansing`）。stg は手で書かない（ADR 0004）。
- Semantic Layer は **latest spec** で書く。model の YAML の中に `semantic_model:` を置き、columns に `entity` / `dimension`、`metrics` を書く（[参考](https://docs.getdbt.com/docs/build/latest-metrics-spec)）。
- サロゲートキーは `dbt_utils.generate_surrogate_key`。日付ディメンションは `dim_date`（`dbt_utils.date_spine`）。
- SCD は Type1（上書き）を基本にする。当時の値で見たい属性は、生成器が dbt snapshot（`snapshots/snp_<表>.yml`）と履歴モデル（`models/marts/dim_<name>_history`）を作る（ADR 0009。規約は [docs/modeling-conventions.md の「SCD」](../docs/modeling-conventions.md#scd)）。
- テスト: 全 dim / fct の PK に unique と not_null、FK に relationships、fct の粒度に `dbt_utils.unique_combination_of_columns`。BEAM の具体例があれば unit test にする。
- 既存の dim は再利用し、カラムの追加のみ許可する。既存カラムの変更や削除はしない。
- 変更したら `dbt build` と `dbt parse` と `mf validate-configs` を実行する。
- サンプルデータは `scripts/` の生成スクリプトで作る（合成データのみ、乱数シードは固定）。
