# モデリング規約

`warehouse/` の dbt モデルの規約。手書きのリファレンス実装（`dim_*` / `fct_order_lines`）と、アプリが生成するモデルの両方に適用する。生成エージェントは、この文書とリファレンス実装をお手本にする。

## レイヤー

| レイヤー | 接頭辞 | マテリアライズ | 役割 |
|---|---|---|---|
| staging | `stg_` | view | ソース1テーブルにつき1モデル。列名の整形、型変換、単純な派生列（例: `sales_amount`）のみ。結合しない |
| intermediate | `int_` | view | 複数の stg を組み合わせる再利用可能な中間処理。必要なときだけ作る |
| marts | `dim_` / `fct_` | table | ディメンショナルモデル。BI と Semantic Layer はここだけを参照する |

集計済みマート（`agg_*`）は作らない。集計は Semantic Layer（MetricFlow）で行う（design-doc R5）。

## BEAM 成果物との対応

| BEAM | dbt |
|---|---|
| ビジネスイベント `customer_orders_product` | `fct_<名詞の複数形>`（例: `fct_order_lines`）。名前はイベントの粒度で付ける |
| story_type: discrete / recurring / evolving | transaction / periodic snapshot / accumulating snapshot |
| grain / grain_keys | fct の主キー。`dbt_utils.unique_combination_of_columns` でテストする |
| measure | fct の数値列 |
| dimension（Who / What / Where / Why / How） | `dim_<name>` と、fct 側の `<name>_key` |
| dimension の role | fct 側の列名を `<role>_key` にし、同じ `dim_<name>` を参照する |
| When | `dim_date` を共有し、fct には日付列（`<role>_on`）と日時列（`<role>_at`）を置く。日付列から `dim_date.date_day` への relationships テストを付ける |
| degenerate dimension | fct に直接置く（`order_id` など） |
| 属性が1つだけでマスタの無い How / Why（例: 支払方法） | dim を作らず fct の列にする |
| example | dbt unit test の期待値 |

## キー

- dim の主キーは `<name>_key`。自然キーから `{{ dim_key('<自然キー>') }}` で作る（`dbt_utils.generate_surrogate_key` のハッシュ）。
- fct の外部キーも、同じ `dim_key()` でイベント側の自然キーから作る。dim と結合して取得しない（結合の漏れで行が消えるのを防ぐ）。
- 自然キーが NULL の場合は不明メンバーのキーになる。各 dim には `{{ unknown_key() }}` の行を1行入れる。表示名は「不明」、意味のある場合は「プロモーションなし」のように書く。
- そのため fct の外部キーは常に NOT NULL になる。

## SCD

- MVP では Type1（上書き）のみ（design-doc R6）。BEAM で `history_required: true` の属性も Type1 で実装し、PR の未解決事項に書く。

## テスト（必須）

| 対象 | テスト |
|---|---|
| すべての dim / fct の主キー | `unique`、`not_null` |
| fct の外部キー | `not_null`、`relationships`（dim へ） |
| fct の粒度 | `dbt_utils.unique_combination_of_columns`（grain_keys） |
| 区分値 | `accepted_values` |
| fct の計算列 | BEAM の具体例から作る unit test |
| fct の件数と金額 | ソースとの突合テスト（`tests/assert_<fct>_reconciles_with_source.sql`） |

## 命名と記述

- テーブル名・列名は英語の snake_case。日付は `_on`、日時は `_at`、金額は `_amount`、フラグは `is_` を付ける。
- description は日本語。金額には「円・税抜」のように単位と税区分を書く。
- fct の description には、元のイベントと粒度を書く。
