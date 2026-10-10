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
- cumulative の `window` は、そのままでは parse に失敗する（"Semantic Manifest validation failed"）。原因は dbt-core 1.12 の latest spec パーサーで、`window` を legacy の `type_params.window` にもコピーするため、既定で true のフラグ `require_nested_cumulative_type_params` の検証に弾かれる。`dbt_project.yml` で `flags: {require_nested_cumulative_type_params: false}` にすると動く（警告 `MFCumulativeTypeParamsDeprecation` が出るのみ）。
- cumulative の `period_agg` の既定は `first`。月・年の粒度で集計すると期間初日の値になるので、`last` を指定する。
- dim の列名に時間粒度の名前（`day` / `week` / `month` / `quarter` / `year`）を使うと、dimension にしたときに検証に失敗する。
- role 付きディメンションや `dim_date` の属性は、`derived_semantics` で役割ごとの entity を定義すれば結合できる。

## 決定

1. MetricFlow + latest spec + DuckDB を採用する（O1 は案 (a) で解決）。
2. cumulative の `window` は上記のフラグで使う。dbt-core のパーサーが直ったらフラグを外す。cumulative には `period_agg: last` を付ける。
3. backend からの指標取得は **`mf` CLI をサブプロセスで呼ぶ**（`mf query ... --csv`）。MetricFlow の Python API は内部 API で、バージョン間の互換性が保証されないため。

## 検討した代替案

- legacy spec（`semantic_models:` と `measures:`）: `window` は使える可能性が高いが、design-doc R12 の決定と dbt の今後の方向に反する。
- MetricFlow の Python API を直接呼ぶ: プロセス起動のコストは無いが、内部 API への依存になる。

## 影響

- `make dbt-build` に、MetricFlow の検証を含める（指標がある場合）。指標は生成物なので、`mf query` での確認は生成の統合テストで行う（#12 / #13）。
- docs/modeling-conventions.md に Semantic Layer の書き方を追加する。
