# 0007: オントロジーの概念と fact / dim を 1 対 1 にする

- 日付: 2026-10-19
- ステータス: Proposed
- 関連: #67、#113、#114、[#137](https://github.com/daikono313/agentic-data-platform/issues/137)、design-doc の R25、[modeling-conventions](../modeling-conventions.md)、ADR [0006](0006-make-ontology-labels-the-business-terms.md)
- 見直し: 同じ概念に粒度の違う fct（例: 注文の明細とヘッダ）が要る要求が続いたとき、または dim_date 以外の共通のモデル（例: 通貨・地域の対応表）が要るとき

## 背景

規則そのものは [modeling-conventions](../modeling-conventions.md) にある。fact / dim はオントロジーの概念と 1 対 1 にし、出来事は fct、もの・人・場所は dim にする。共通の暦（`dim_date`）だけは概念を持たない。

実装は 3 回に分けて入った。

- #67（v3、2026-10-11）: 概念の種類とモデルの接頭辞を合わせる検査、1 つのモデルを複数の概念に結び付けない検査。
- #113（v4、2026-10-12）: 設計の段階で、同じ出来事の 2 つ目の fact を止める検査。
- #114（v4、2026-10-12）: どの概念にも結び付かない fct / dim をエラーにする検査。

どれも、なぜそう決めたかと、どんな案を比べたかは書かれていない。オーナーの指示（2026-10-12、#137）により、この ADR に残す。#114 の前は、同じ用途の fact を 2 つ作れた。

## 判断の基準

- **用途の重複が無いこと（C11）。** 同じ用途のモデルを増やさない。インタビューと生成が「既存の fact → データ全体 → 新規作成」の順に探せる（#113）ように、用途ごとのモデルが 1 つに決まる。
- **機械で止められること。** 設計の段階（確定前の課題）と `make dbt-build` の検査で止める。
- **Kimball の考え方に沿うこと。** 出来事（業務プロセス）は fact、もの・人・場所は dimension（[modeling-conventions](../modeling-conventions.md)）。

## 検討した選択肢

### 案 A: 概念とモデルを 1 対 1 にし、種類で fct / dim を決める（採用）

- 良い点:
  - 概念から、用途のモデルが 1 つに決まる。そのため、同じ用途の 2 つ目の fct / dim を、名前ではなく概念で見つけられる。
  - 取り込んだ源泉は、まず概念になる。その後に作るモデルが、その概念に結び付く（#69）。
- 悪い点:
  - 同じ出来事に粒度の違う fct が要るときは、概念を分ける必要がある。例えば、注文のヘッダと明細を、それぞれの概念にする。
  - どの概念にも属さないモデル（暦）は、例外として名前で持つ（`SHARED_MODELS`）。

### 案 B: 1 つの概念に、粒度・用途ごとの複数のモデルを許す

- 良い点: 概念を分けずに、集計済みの fct や粒度の違う fct を足せる。
- 悪い点:
  - どのモデルを使うかが概念から決まらず、同じ用途の fct が増える（#114 の前の状態）。
  - 集計は Semantic Layer に一元化する（R5）ので、集計済みの fct は要らない。そのため採らない。

### 案 C: モデルを概念に結び付けず、名前の規約（`fct_<出来事>`・`dim_<もの>`）だけで管理する

- 良い点: オントロジーを持たなくても動く。
- 悪い点:
  - 名前の違う同じ用途のモデル（例: `fct_sales` と `fct_order_lines`）を見つけられない。
  - 源泉から用途をたどれない。そのため採らない。

### 案 D: 暦も概念にする

- 良い点: 例外が無くなる。
- 悪い点:
  - 暦は源泉を持たない。オントロジーの概念は源泉とキーを持つ（`Concept.source`・`Concept.key`）ので、型に合わない。そのため採らない。

## 決定

案 A を採る。判断の基準の「用途の重複が無いこと」を優先した。粒度の違う fct で概念を分ける手間は、確定前の課題がそうするよう示すので受け入れる。

## 結果

- 良い影響:
  - 同じ用途の 2 つ目の fct / dim が、確定の前か CI で止まる。
  - オントロジーの図から、整備済みのモデルが分かる。
- 悪い影響・トレードオフ:
  - 粒度の違う fct は、概念を分ける必要がある。
  - 共通のモデルを足すときは、`SHARED_MODELS` と規約を直す必要がある。
- 後続の作業・ADR: 階層と関係の多重度をオントロジーに置くこと（#137）。

## 守らせ方

- `make dbt-build` が、`python -m adp.ontology check` を実行する（`Makefile`）。CI で落ちる。
- 種類と接頭辞: `backend/tests/ontology/test_ontology.py` の `test_model_must_match_kind`
- 1 つのモデルは 1 つの概念: `backend/tests/ontology/test_ontology.py` の `test_duplicates_are_rejected`、`backend/tests/onboarding/test_onboarding.py` の `test_link_models_keeps_one_concept_per_model`
- 概念の無いモデルは作らない: `backend/tests/ontology/test_ontology.py` の `test_model_without_concept_is_an_error`
- 同じ概念の 2 つ目のモデル（確定前の課題）: `backend/tests/design/test_reuse.py` の `test_duplicate_fact_of_the_same_type_points_to_the_existing_fact`、`test_duplicate_fact_of_another_type_needs_its_own_concept`

## 追記
