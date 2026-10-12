# モデリング規約

`warehouse/` の dbt モデルの規約。既存の共通ディメンション（`dim_*`）と、アプリが生成するモデルの両方に適用する。**この文書は人間が持ち主**で、生成器（`backend/src/adp/generation/`。AI が書くコード）はこの文書をテンプレートとして実装している。規約を変えるときは、まずこの文書を直し、それに合わせて生成器を直す。リファレンス fct（`backend/tests/fixtures/generation/reference/`。生成器とは別に書いたもの）は、生成結果と行単位で一致することをテストで確認している。整備済みの dim / fct（手で書いた共通の dim を含む）が規約に合っているかは、`make dbt-build` の最後に決まった規則で検査する（`backend/src/adp/generation/conventions.py`。主キー・粒度・外部キーのテスト、role の entity、日付・日時・フラグの列名、不明メンバーの行）。

## レイヤー

| レイヤー | 接頭辞 | マテリアライズ | 役割 |
|---|---|---|---|
| staging（クレンジング層） | `stg_` | view | ソース1テーブルにつき1モデル。**クレンジングの定義（`warehouse/cleansing/<schema>.<table>.yml`）から生成する**（`make cleansing`。手で書かない。ADR 0004）。型変換・trim・空文字を NULL・表記の統一・値の対応表・日付の切り出し・行の除外・重複の除去まで。計算や結合はしない |
| intermediate | `int_` | view | 複数の stg を組み合わせる再利用可能な中間処理。必要なときだけ作る |
| marts | `dim_` / `fct_` | table | ディメンショナルモデル。BI と Semantic Layer はここだけを参照する |

集計済みマート（`agg_*`）は作らない。集計は Semantic Layer（MetricFlow）で行う（design-doc R5）。

## 新しい指標の作り方（探す順序。#113）

用途が同じ fact や dimension を作らない。新しい指標（Semantic Layer）は、次の順に探して作る。

1. **既存の fact**: 整備済みの fact に、使える項目（measure・切り口）があれば、その fact に指標を足す（イベントの `table_name` を既存の fact にそろえる）
2. **データ全体**: 無ければ、源泉（未整理のものを含む）にあるかを探す
3. **新規作成**: 2 で見つかれば、その源泉のクレンジング層（stg）と fact を新しく作る。源泉がオントロジーに無ければ、先に取り込む（オントロジーへの追加。CLAUDE.md の規約）

fact / dim はオントロジーの概念と 1 対 1 にする（出来事 → fct、もの・人・場所 → dim）。同じ概念の 2 つ目のモデルは作らない（設計の段階で止まり、`make dbt-build` の検査でも落ちる）。共通の暦（`dim_date`）だけは概念を持たない。

## BEAM 成果物との対応（生成ルール）

BEAM 成果物（`backend/src/adp/beam/models.py`）から、次のルールで機械的に名前と構造を決める。例は `backend/tests/fixtures/beam/sales_by_store.yml` → `fct_order_lines`。

### イベント → fct

| BEAM | dbt |
|---|---|
| `event.table_name`（必須） | `fct_<table_name>`（例: `order_lines` → `fct_order_lines`） |
| `story_type` | discrete → transaction、recurring → periodic snapshot、evolving → accumulating snapshot |
| `measures[].source` | fct の列 `<measure.name>`。`column` ならそのまま、`expr` なら **fct の SQL で計算する**（staging では計算しない） |
| `degenerate_dimensions[]` | fct の列 `<name>`（source の列を改名） |
| `grain_keys` | fct の主キー。縮退ディメンションならその列、dimension ref なら後述のキー列。複数あれば `dbt_utils.unique_combination_of_columns`、1つなら `unique` |

### ディメンション → dim と fct の列

ディメンションは次の3種類に分ける。

| 種類 | 判定 | dim | fct の列 |
|---|---|---|---|
| 通常 | When 以外で、`business_key.table` が `fact_key.table` と異なる | `dim_<name>`（主キー `<name>_key`、列は属性名） | `<ref>_key` = `{{ dim_key('<fact_key>') }}` |
| マスタなし | When 以外で、`business_key` と `fact_key` が同じ列 | 作らない | `<ref>` = fact_key の列（属性はこの1列だけを持つ） |
| When | `w: when` | `dim_date` を共有 | 下記 |

- `<ref>` は `role` があれば role、無ければ name。
- When の列: fact_key の列名が `_at` で終わるなら、日時列 `<base>_at` と日付列 `<base>_on`（`cast(... as date)`）を置く（例: `ordered_at` → `ordered_at` / `ordered_on`）。fact_key が expr か、`_at` で終わらない列なら、`<ref>_at` / `<ref>_on` にする。日付列から `dim_date.date_day` への relationships テストを付ける。
- 同じ dim を role 違いで複数回使う場合は、`<role>_key` を複数置き、どれも同じ `dim_<name>` を参照する。

### 具体例（examples） → unit test

- unit test の対象は fct（計算は fct で行うため）。
- measure と縮退ディメンションのキーは、`expect` の期待値にする。
- dim の属性（`<ref>.<attr>`）と When のキーは、`given` の入力行（stg）を組み立てるのに使い、fct の出力では検証しない。
- 入力行に必要で具体例に無い列は、テストが成り立つ最小の値で補う（例: 値引 0）。
- 外部キーが NULL の入力を1行入れ、不明メンバーのキー（`md5('__unknown__')`）になることも検証する。

## キー

- dim の主キーは `<name>_key`。自然キーから `{{ dim_key('<自然キー>') }}` で作る（`dbt_utils.generate_surrogate_key` のハッシュ）。NULL は番兵値 `__unknown__` に置き換えてからハッシュする。
- fct の外部キーも、同じ `dim_key()` でイベント側の自然キー（fact_key）から作る。dim と結合して取得しない（結合の漏れで行が消えるのを防ぐ）。
- 自然キーが NULL の場合は不明メンバーのキーになる。各 dim には `{{ unknown_key() }}` の行を1行入れる。
  - 不明メンバー行の文字列の属性は「不明」にする。業務上の意味がある場合は、その意味を表す値にする（例: プロモーションの `promotion_name` は「プロモーションなし」、`promotion_type` は `none`）。
  - 区分値の `accepted_values` には、不明メンバー行の値も含める。
- そのため transaction fact の外部キーは常に NOT NULL になる。
- accumulating snapshot のマイルストーン日付（例: `delivered_on`）は、イベントが起きるまで NULL になるので、`not_null` を付けず relationships だけを付ける。

## dim_date

- 範囲は `dbt_project.yml` の vars（`adp_date_start` / `adp_date_end`）で決める。When のソースの日付がすべて収まるようにし、収まらない場合は vars を広げる。

## SCD

- MVP では Type1（上書き）のみ（design-doc R6）。BEAM で `history_required: true` の属性も Type1 で実装し、PR の未解決事項に書く。

## テスト（必須）

| 対象 | テスト |
|---|---|
| すべての dim / fct の主キー | `unique`、`not_null` |
| fct の外部キー | `relationships`（dim へ）。transaction fact では `not_null` も付ける |
| fct の粒度 | grain_keys が1つなら主キーの `unique`、複数なら `dbt_utils.unique_combination_of_columns` |
| 区分値 | `accepted_values`（不明メンバー行の値も含める） |
| fct の計算列 | BEAM の具体例から作る unit test |
| fct の件数と金額 | ソースとの突合テスト（`tests/assert_<fct>_reconciles_with_source.sql`） |

## 命名と記述

- テーブル名・列名は英語の snake_case。日付は `_on`、日時は `_at`、金額は `_amount`、フラグは `is_` を付ける。
- description は日本語。金額には「円・税抜」のように単位と税区分を書く。
- fct の description には、元のイベントと粒度を書く。

## Semantic Layer（MetricFlow, latest spec）

[ADR 0001](adr/0001-metricflow-on-duckdb.md) を参照。dim の例は `warehouse/models/marts/_marts.yml`、fct と指標の例は `backend/tests/fixtures/generation/reference/fct_order_lines.yml`。

### semantic model

- dim / fct の model YAML に `semantic_model: {enabled: true}` を書く。
- dim の主キーは `entity: {type: primary, name: <dimension name>}`、属性は `dimension: {type: categorical}`。
- fct
  - 外部キー `<name>_key` は `entity: {type: foreign, name: <dimension name>}`。
  - grain_keys が縮退ディメンション1つなら、その列を `entity: {type: primary, name: <fct の単数形>}` にする（例: `order_line`）。grain_keys が複数なら、model に `primary_entity: <fct の単数形>` を書く。
  - 縮退ディメンション、マスタなしのディメンション、区分値の列は `dimension: {type: categorical}`。
  - When の日付列に `granularity: day` と `dimension: {type: time}` を書き、model に `agg_time_dimension: <列名>` を置く。
- role 付きディメンション（例: 配送先顧客 `ship_to`）は `derived_semantics` で役割ごとの entity を作る。
  - dim 側: `derived_semantics: {entities: [{type: unique, name: <role>, expr: <name>_key}]}`
  - fct 側: `derived_semantics: {entities: [{type: foreign, name: <role>, expr: <role>_key}]}`
- When と `dim_date`
  - `dim_date` は time spine を兼ね、`date_day` を `entity: {type: primary, name: date}` にする（属性は categorical）。
  - fct の agg_time の日付列について `derived_semantics: {entities: [{type: foreign, name: date, expr: <日付列>}]}` を書く（MVP では agg_time の When だけ dim_date の属性で集計できる）。
- dim の列名に `day` / `week` / `month` / `quarter` / `year` を使わない（時間粒度の名前と衝突して検証に失敗する）。

### metric

- simple はその fct の model YAML 内に書く（`agg` / `expr` / `filter`）。ratio / derived / cumulative は、その fct の YAML ファイルのトップレベル `metrics:` に書く。
- cumulative は `window`（例: `7 days`）か `grain_to_date` を使い、必ず `period_agg: last` を付ける。
- 業務上の除外条件（キャンセルを除くなど）は `filter` に書き、description にも日本語で書く。ratio の分子と分母で条件をそろえる。
- BEAM の `MetricRequest.filters`（ref・演算子・値）を `{{ Dimension('<参照名>') }} <演算子> <値>` に変換する（すべて AND）。ratio / derived の filter は、その指標自体に付ける。
- 累計（cumulative）の元になる simple metric は `<name>_input` という名前で自動で作る（この名前は予約）。
- 粒度のキー（主 entity）で切る場合の参照名は entity 名そのもの（例: `order_line`）。

### チャート（dbt Charts）

- チャートは `warehouse/chart_specs/<board>.yml` に、指標・切り口・並び順だけで書く（SQL を書かない）。`make charts` で dbt Charts のボードを書き出す（ADR 0002）。
- ボードの x / y / color には MetricFlow の参照名（`store__store_name`、`metric_time__month`、指標名）を使う。
- 生成パイプラインは、確定した指標ごとに既定のチャートを 1 つ作る（`warehouse/chart_specs/<beam>.yml`、#42）。
  - 時間の切り口がある → 折れ線（x: 時間、color: 最初のカテゴリの切り口）
  - 比率で時間の切り口が無い → 月別の折れ線
  - カテゴリの切り口だけ → 棒（切り口が 1 つなら上位 20 件、2 つ目は color）
  - 切り口が無い → KPI
- 同じ名前の元ファイルかボードが既にあれば、作らない（手で整えたチャートを上書き・削除しない）。理由は PR 本文に書く。
- 生成 PR の本文には、ボードを PNG に描画した画像を載せる。画像は PR のブランチで commit した直後の commit で消し、PR 本文は画像のある commit を参照する（main に画像を残さない）。描画できなければ、チャートを含めずに理由を PR 本文に書く。

### BEAM の group_by → MetricFlow の参照名

| BEAM | MetricFlow |
|---|---|
| `<ref>.<attr>`（通常のディメンション） | `<ref>__<attr>` |
| `<ref>.<attr>`（マスタなしのディメンション。属性名は無視する） | `<fct の主 entity>__<ref の列名>`（例: `order_line__payment_method`） |
| 縮退ディメンション名 | `<fct の主 entity>__<名前>` |
| `<agg_time_dimension の ref>` / `<ref>.<grain>` | `metric_time__<grain>`（grain 省略時は When の time_grain） |
| それ以外の When の `<ref>.<grain>` | `<fct の主 entity>__<日付列>__<grain>` |
| When の属性（曜日など）`<When ref>.<attr>` | `<When ref>__<attr>` |

- cumulative の metric は、`metric_time__<grain>` を group_by に含めないとクエリできない。生成時の確認クエリでは必ず含める。
