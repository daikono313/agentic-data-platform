# 0001: MetricFlow（latest spec）を DuckDB で使う

- 日付: 2026-10-10
- ステータス: Accepted
- 関連: #6、design-doc の O1（→ R16）、ADR 0003
- 見直し: dbt-core の latest spec パーサーが cumulative の `window` を正しく扱うようになったとき（フラグを外す）

## 背景

design-doc R12 で、Semantic Layer は OSS の MetricFlow を latest spec で使うと決めた。DuckDB で動くかは未検証だった（O1）。

検証の結果（dbt-core 1.12.5 / dbt-duckdb 1.11.0 / dbt-metricflow 0.15.0。2026-10-10）:

- model YAML 内の `semantic_model:`、columns の `entity` / `dimension`、`granularity`、`agg_time_dimension`、モデル内の `metrics`（simple: sum / count / count_distinct、`filter` 付き）、トップレベルの ratio が、`dbt parse` と `mf validate-configs`（DWH 検証込み）を通る。
- time spine は `dim_date` に `time_spine.standard_granularity_column` を設定して兼用できる。
- `mf query` で、dim の属性による group by（`store__channel`）、`metric_time__month`、`--where`、`--csv` が動く。
- cumulative の `window` は、そのままでは parse に失敗する（"Semantic Manifest validation failed"）。原因は dbt-core 1.12 の latest spec パーサーで、`window` を legacy の `type_params.window` にもコピーするため、既定で true のフラグ `require_nested_cumulative_type_params` の検証に弾かれる。`dbt_project.yml` で `flags: {require_nested_cumulative_type_params: false}` にすると動く（警告 `MFCumulativeTypeParamsDeprecation` が出るのみ）。
- cumulative の `period_agg` の既定は `first`。月・年の粒度で集計すると期間初日の値になる。
- dim の列名に時間粒度の名前（`day` / `week` / `month` / `quarter` / `year`）を使うと、dimension にしたときに検証に失敗する。
- role 付きディメンションや `dim_date` の属性は、`derived_semantics` で役割ごとの entity を定義すれば結合できる。

## 判断の基準

- design-doc R12（latest spec）と dbt の今後の方向に沿う
- ローカルの DuckDB だけで、Semantic Layer の定義・検証・取得が完結する
- バージョンを上げたときに壊れにくい（内部 API に依存しない）

## 検討した選択肢

### 案 A: latest spec + DuckDB、取得は `mf` CLI（採用）

- 良い点: R12 に沿う。必要な指標の型が DuckDB で動く。CLI は利用者向けのインターフェースで、バージョン間で安定している。
- 悪い点: cumulative の `window` にフラグが要る。CLI はプロセス起動のコストがかかる。

### 案 B: legacy spec（`semantic_models:` と `measures:`）

- 良い点: `window` はフラグなしで使える可能性が高い。
- 悪い点: design-doc R12 の決定と dbt の今後の方向に反する。

### 案 C: 取得に MetricFlow の Python API を直接呼ぶ

- 良い点: プロセス起動のコストが無い。
- 悪い点: 内部 API で、バージョン間の互換性が保証されない。

## 決定

案 A を採る（O1 は案 (a) で解決）。

1. MetricFlow + latest spec + DuckDB を使う。
2. cumulative の `window` は上記のフラグで使う。cumulative には `period_agg: last` を付ける。
3. backend からの指標取得は `mf` CLI をサブプロセスで呼ぶ（`mf query ... --csv`）。

## 結果

- 良い影響: Semantic Layer の定義から取得までをローカルで確かめられる。
- 悪い影響・トレードオフ: フラグを立てている間は、`MFCumulativeTypeParamsDeprecation` の警告が出る。CLI の起動に時間がかかる（描画画面で問題になり、ADR 0003 で一部を変えた）。
- 後続の作業・ADR: docs/modeling-conventions.md に Semantic Layer の書き方を追加した。指標は生成物なので、`mf query` での確認は生成の統合テストで行う（#12 / #13）。

## 守らせ方

- `make dbt-build` で `mf validate-configs` を実行する（指標がある場合）。
- `warehouse/dbt_project.yml` のフラグに、この ADR を指すコメントを置いている。

## 追記

- 2026-10-11: 描画画面とデータ一覧の切り口は、速さのために MetricFlow をプロセス内で使うことにした（決定の 3 を一部変更。[ADR 0003](0003-chart-view-in-process.md)）。生成パイプライン・`adp.charts compile / check`・検証は CLI のまま。
- 2026-10-16: ADR のテンプレートの変更に合わせて構成を直した（決定の中身は変えていない）。
