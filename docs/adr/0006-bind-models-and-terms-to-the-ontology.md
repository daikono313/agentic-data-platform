# 0006: fact / dim と業務の言葉をオントロジーに結び付ける

- 日付: 2026-10-19
- ステータス: Proposed
- 関連: #137、#114、#67、#69、design-doc の R24、[modeling-conventions](../modeling-conventions.md)
- 見直し: 実データに切り替えるとき（源泉が増え、同じ言葉・同じ値の列が増えるため）、または同じ概念に粒度の違う fact が要る要求が続いたとき

## 背景

オントロジー（`ontology/ontology.yml`）は v3 で、業務の概念・属性・関係を整理するために入れた（#67・#69）。ただ、v4 までは次のことが決まっていなかった。

- fact / dim（dbt のモデル）とオントロジーの概念が、どう対応するか。
- BEAM 成果物（`beam/*.yml`）・dbt の説明・オントロジーが、それぞれ別々に言葉（ラベル）を持つこと。

その結果、次の食い違いが起きていた（#137 を起票した時点。2026-10-12）。

- 同じ地方の列が、オントロジーでは「居住地方」「所在地方」、BEAM では「地方」と呼ばれていた。
- 支払方法（`payment_method`）が、注文と支払の 2 つの概念に、結び付かないまま定義されていた。
- 意味の違う 2 つの「数量」（販売と入出庫）が、同じラベルだった。
- 同じ用途の fact を 2 つ作れた。#114 で、概念とモデルを 1 対 1 にして止めた。

これらの規則は、モデリング規約・コード（`adp.ontology`・`adp.design`）・issue に分かれて書かれている。オーナーの指示（2026-10-12、#137）により、なぜそう決めたかを 1 か所に残す。

## 判断の基準

- **言葉が 1 つに決まること。** インタビュー・生成した PR・画面で、同じものを同じ言葉で呼ぶ（R1: ビジネスユーザー向け）。
- **用途の重複が無いこと（C11）。** 同じ用途のモデル・定義を増やさない。
- **機械で止められること。** 人の注意ではなく、`make ontology` / `make dbt-build` と、確定前の課題（設計の検査）で止める。
- **生成した PR が自分の CI で落ちないこと。** 設計の検査と CI の検査の判定をそろえる。

## 検討した選択肢

### 案 A: オントロジーを正本にし、モデルと言葉をそこへ結び付ける（採用）

次の 4 つを規則にする。

1. 概念の種類でモデルの種類を決める。出来事（event）は fct、もの・人・場所（entity）は dim にする。
2. 概念とモデルは 1 対 1 にする。どの概念にも結び付かない fct / dim は作らない。例外は、共通の暦 `dim_date` だけ（どの概念にも属さない）。
3. 属性のラベルを、業務の言葉の正本にする。次の 2 つは、それにそろえる。
   - BEAM の属性のラベル（源泉の列が同じもの。値を置き換えるだけの式も含む）
   - dim の列の説明（ラベルそのままか、「ラベル（補足）」）
4. 同じラベルは、1 つの言葉に 1 つの正本にする。
   - 同じ値を別の源泉にも持つときは、`same_as` で正本の属性を示す（例: 注文の支払方法 → 支払の支払方法）。
   - 意味が違うときは、ラベルを分ける（例: 販売数量・入出庫数量）。

- 良い点:
  - 言葉と用途の正本が 1 か所になる。
  - 食い違いを、設計の段階（確定前の課題）と CI の両方で止められる。
  - 取り込み（#69）で増えた源泉も、同じ規則で検査される。
- 悪い点:
  - `ontology.yml` のラベルを変えると、そのラベルを使う BEAM 成果物と dim の説明も直す必要がある。直すまで CI が落ちる（意図した動き）。
  - 同じ概念に粒度の違う fct（例: 注文の明細と注文のヘッダ）が要るときは、概念を分ける必要がある。

### 案 B: 言葉は BEAM 成果物ごとに持ち、オントロジーは図にだけ使う

- 良い点:
  - 要求ごとに、ユーザーの言い方をそのまま使える。
  - ラベルを変えても、ほかの成果物に影響しない。
- 悪い点:
  - 同じ列が要求ごとに違う名前になる。共通ディメンション（conformed dimension）の列の説明が、最初に作った要求の言葉に固定される。
  - 今の食い違い（地方・数量）を止められない。
  - 判断の基準の「言葉が 1 つに決まること」を満たさないので、採らない。

### 案 C: 言葉は dbt の説明（`_marts.yml`・生成した yml）を正本にし、オントロジーと BEAM を dbt に合わせる

- 良い点:
  - 実際に問い合わせる Semantic Layer の言葉と一致する。
- 悪い点:
  - まだモデルの無い概念（取り込んだ直後の源泉、fct の無い出来事）は、言葉を持てない。
  - fct の説明は measure のラベル（「値引額(税抜)」など）から作るので、補足と言葉を分けられない。
  - 正本が生成物の側になり、「生成物は手で直さない」（design-doc の「ファイルの持ち主」）と食い違う。このため採らない。

### 案 D: 同じ値の列に、正本を示さず同じラベルを付けることを許す（`same_as` を作らない）

- 良い点: スキーマが増えない。
- 悪い点:
  - 同じラベルが、同じものか、偶然の重なり（意味の違う「数量」）かを区別できず、機械で止められない。
  - インタビューで「支払方法」と言われたとき、どの源泉の列かが決まらない。このため採らない。

## 決定

案 A を採る。判断の基準のうち、「言葉が 1 つに決まること」と「機械で止められること」を優先した。悪い点（ラベルを変えたときの追従と、粒度の違う fct で概念を分けること）は、CI と確定前の課題が何を直すかを示すので、受け入れる。

1 と 2 は、すでにモデリング規約と #114 の実装で守っている。この ADR は、その理由を記録する。3 と 4 は #137（#194・#195）で入れた。

## 結果

- 良い影響:
  - インタビューで確定する BEAM の言葉が、オントロジーと dim の説明と一致する。
  - 同じ用途の 2 つ目の fact、言葉の二重定義、ラベルの食い違いが、確定の前か CI で止まる。
- 悪い影響・トレードオフ:
  - `ontology.yml` のラベルを変える PR は、使っている BEAM 成果物と dim の説明も直す。
  - 列から別の言葉を作る式（年代・価格帯など）と、名前が列名と違う dim の列の説明は照合しない（#195 の範囲外）。
  - fct の列の説明は照合しない。measure のラベルに補足が入るため。
- 後続の作業・ADR:
  - 属性の型・役割・同義語（#137）
  - 階層と関係の多重度（#137）
  - 生成の検証（`verify`）でもオントロジーを照合すること

## 守らせ方

- `make dbt-build` と `make ontology` が、`python -m adp.ontology check` を実行する（`Makefile`）。CI で落ちる。
- 規則 1（概念の種類とモデルの種類）: `backend/tests/ontology/test_ontology.py` の `test_model_must_match_kind`
- 規則 2（概念とモデルは 1 対 1）:
  - `backend/tests/ontology/test_ontology.py` の `test_model_without_concept_is_an_error`
  - `backend/tests/onboarding/test_onboarding.py` の `test_link_models_keeps_one_concept_per_model`
  - `backend/tests/design/test_reuse.py` の `test_duplicate_fact_of_the_same_type_points_to_the_existing_fact`
  - `backend/tests/design/test_reuse.py` の `test_duplicate_fact_of_another_type_needs_its_own_concept`
- 規則 3（言葉の照合）:
  - `backend/tests/ontology/test_ontology.py` の `test_beam_words_must_match_ontology`
  - `backend/tests/ontology/test_ontology.py` の `test_dim_descriptions_start_with_the_label`
  - `backend/tests/design/test_reuse.py` の `test_attribute_words_must_match_ontology`
  - `backend/tests/design/test_reuse.py` の `test_dim_column_names_must_keep_the_ontology_word`
- 規則 4（1 つの言葉に 1 つの正本）:
  - `backend/tests/ontology/test_ontology.py` の `test_same_as_names_the_canonical_attribute`
  - `backend/tests/ontology/test_ontology.py` の `test_duplicate_labels_fail_the_check`

## 追記
