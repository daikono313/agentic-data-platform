# 0001: MetricFlow（latest spec）を DuckDB で使う

- 日付: 2026-10-10
- ステータス: Accepted
- 関連: #6、design-doc の O1（→ R16）

## 背景

design-doc R12 で、Semantic Layer は OSS の MetricFlow を latest spec で使うと決めた。DuckDB で動くかは未検証だった（O1）。

## 検証結果（dbt-core 1.12.5 / dbt-duckdb 1.11.0 / dbt-metricflow 0.15.0）

- model YAML 内の `semantic_model:`、columns の `entity` / `dimension`、`granularity`、`agg_time_dimension`、モデル内の `metrics`（simple: sum / count / count_distinct、`filter` 付き）、トップレベルの ratio が、`dbt parse` と `mf validate-configs`（DWH 検証込み）を通る。
- time spine は `dim_date` に `time_spine.standard_granularity_column` を設定して兼用できる。
- `mf query` で、dim の属性による group by（`store__channel`）、`metric_time__month`、`--where`、`--csv` が動く。
- **制約: cumulative の `window` が parse に失敗する。** `window: 30 days`、`30d`、`cumulative_type_params` の入れ子、オブジェクト形式、`period_agg` 付きのいずれも "Semantic Manifest validation failed" になる。`grain_to_date` と全期間累計は動く。

## 決定

1. MetricFlow + latest spec + DuckDB を採用する（O1 は案 (a) で解決）。
2. cumulative は `grain_to_date` と全期間累計のみ使う。BEAM の `MetricRequest.window` が指定された場合、生成時（#13）は gap（deferred）として記録し、PR の未解決事項に書く。dbt / MetricFlow の更新で直ったら見直す。
3. backend からの指標取得は **`mf` CLI をサブプロセスで呼ぶ**（`mf query ... --csv`）。MetricFlow の Python API は内部 API で、バージョン間の互換性が保証されないため。

## 検討した代替案

- legacy spec（`semantic_models:` と `measures:`）: `window` は使える可能性が高いが、design-doc R12 の決定と dbt の今後の方向に反する。
- MetricFlow の Python API を直接呼ぶ: プロセス起動のコストは無いが、内部 API への依存になる。

## 影響

- `make dbt-build` に、MetricFlow の検証とスモーククエリ（`mf query`）を含める。
- docs/modeling-conventions.md に Semantic Layer の書き方を追加する。
