# 0006: オントロジーの属性のラベルを業務の言葉の正本にする

- 日付: 2026-10-19
- ステータス: Proposed
- 関連: [#137](https://github.com/daikono313/agentic-data-platform/issues/137)、#194、#195、design-doc の R24、ADR [0007](0007-map-each-concept-to-one-model.md)
- 見直し: 実データに切り替えるとき（同じ言葉・同じ値の列が増えるため）、または文脈ごとに別の言い方が要る要求が続いたとき（同義語を扱う #137 の後続で）

## 背景

業務の言葉（日本語のラベル）は、次の 3 か所が別々に持っていた。

- オントロジー（`ontology/ontology.yml`）の属性
- BEAM 成果物（`beam/*.yml`）の属性
- dbt の dim の列の説明（`warehouse/models/marts/_marts.yml` と、生成した yml）

これらを照合していなかったので、次の食い違いがあった（#137 を起票した 2026-10-12 から、#195 で直すまで）。

- 店舗の地方（`raw.stores.region`）が、オントロジーでは「所在地方」、`beam/inventory_status.yml` では「地方」だった。
- 支払方法（`payment_method`）が、注文（`raw.orders`）と支払（`raw.payments`）の 2 つの概念に、どちらが正本か示されないまま、同じラベルで定義されていた。サンプルデータでは同じ値を持つ。
- 意味の違う 2 つの「数量」（注文明細の販売数量と、在庫の入出庫の数量）が、同じラベルだった。

ラベルは、指標の切り口・バスマトリクス・開始画面・インタビューの呼び方として画面に出る。言葉がずれると、ユーザーにとって同じものが違う名前で見える（R1）。

## 判断の基準

- **言葉が 1 つに決まること。** インタビュー・生成した PR・画面で、同じものを同じ言葉で呼ぶ（R1）。
- **正本を人間が確定できること。** 言葉は業務の決定なので、人間が確定するファイルに置く（design-doc の「ファイルの持ち主」）。
- **機械で止められること。** 人の注意ではなく、`make ontology` / `make dbt-build` と、確定前の課題（設計の検査）で止める。
- **生成した PR が自分の CI で落ちないこと。** 設計の検査と CI の検査の判定をそろえる。

## 検討した選択肢

### 案 A: オントロジーの属性のラベルを正本にし、BEAM と dim の説明をそろえる（採用）

- オントロジーの属性のラベルにそろえるもの（2 つ）:
  - BEAM の属性のラベル。オントロジーの属性と、源泉のテーブル・列が同じもの。値を置き換えるだけの式（`coalesce(列, 'リテラル')` など）も含む。
  - dim の列の説明。ラベルそのままか、「ラベル（補足）」にする。
- 同じラベルの属性が別々にあってはいけない。
  - 同じ値を別の源泉にも持つときは、`same_as` で正本の属性を示す（例: 注文の支払方法 → 支払の支払方法）。
  - 意味が違うときは、ラベルを分ける（例: 販売数量・入出庫数量）。
- 良い点:
  - 言葉の正本が 1 か所になる。そこは、人間が確定するファイル（オントロジー）。
  - 食い違いを、設計の段階（確定前の課題）と CI の両方で止められる。
  - 取り込み（#69）で増えた源泉も、同じ規則で検査される。
- 悪い点:
  - `ontology.yml` のラベルを変えると、そのラベルを使う BEAM 成果物と dim の説明も直す必要がある。直すまで CI が落ちる（意図した動き）。
  - 列ごとにラベルが 1 つに決まり、文脈ごとに別の言い方はできない。

### 案 B: 言葉は BEAM 成果物ごとに持ち、オントロジーは図にだけ使う

- 良い点:
  - 要求ごとに、ユーザーの言い方をそのまま使える。
  - ラベルを変えても、ほかの成果物に影響しない。
- 悪い点:
  - 同じ列が、要求ごとに違う名前になる。
  - 共通ディメンション（conformed dimension）の列の説明が、最初に作った要求の言葉に固定される。
  - 上の食い違いを止められない。「言葉が 1 つに決まること」を満たさないので、採らない。

### 案 C: 言葉は dbt の説明を正本にし、オントロジーと BEAM を dbt に合わせる

- 良い点:
  - 実際に問い合わせる Semantic Layer の言葉と一致する。
- 悪い点:
  - まだモデルの無い概念（取り込んだ直後の源泉、fct の無い出来事）は、言葉を持てない。
  - どちらのファイルも、人間が確定する言葉の正本にならない（design-doc の「ファイルの持ち主」）。
    - 生成した fct の yml は生成物で、手で直さない。
    - `_marts.yml` は AI が書く「データ基盤の共通部分」。
  - fct の説明は measure のラベル（「値引額(税抜)」など）から作るので、補足と言葉を分けられない。このため採らない。

### 案 D: 同じ値の列に、正本を示さず同じラベルを付けることを許す（`same_as` を作らない）

- 良い点: スキーマが増えない。
- 悪い点:
  - 同じラベルが、同じものか、偶然の重なり（意味の違う「数量」）かを区別できない。そのため、機械で止められない。
  - インタビューで「支払方法」と言われたとき、どの源泉の列かが決まらない。このため採らない。

## 決定

案 A を採る。判断の基準のうち、「言葉が 1 つに決まること」と「正本を人間が確定できること」を優先した。ラベルを変えたときの追従は、CI と確定前の課題が、何を直すかを示すので受け入れる。

実装は #194（BEAM の属性のラベル）と #195（`same_as`・同じラベルの検出・式の属性・dim の説明）。

## 結果

- 良い影響:
  - インタビューで確定する BEAM の言葉が、オントロジーと dim の説明と一致する。
  - 言葉の二重定義とラベルの食い違いが、確定の前か CI で止まる。
- 悪い影響・トレードオフ:
  - `ontology.yml` のラベルを変える PR は、使っている BEAM 成果物と dim の説明も直す必要がある。
  - 次のものは照合しない（#195 の範囲外）。
    - 列から別の言葉を作る式（年代・価格帯など）
    - 名前が列名と違う dim の列の説明
    - fct の列の説明（measure のラベルに補足が入るため）
- 後続の作業・ADR（どれも #137）:
  - 属性の型・役割・同義語
  - ラベルの命名の決まり。規約の文書なので T3（オーナーの指示、2026-10-13 の #137 のコメント）
  - 列名の決まりと名前の照合（#134 から移したもの）
  - 生成の検証（`verify`）でも、オントロジーを照合すること

## 守らせ方

- `make dbt-build` と `make ontology` が、`python -m adp.ontology check` を実行する（`Makefile`）。CI で落ちる。
- BEAM の属性のラベル（CI）: `backend/tests/ontology/test_ontology.py` の `test_beam_words_must_match_ontology`、`test_beam_words_check_single_column_expressions`
- BEAM の属性のラベル（確定前の課題）: `backend/tests/design/test_reuse.py` の `test_attribute_words_must_match_ontology`、`test_expression_attribute_words_must_match_ontology`、`test_derived_attributes_are_not_forced_to_the_source_label`
- dim の列の説明:
  - CI: `backend/tests/ontology/test_ontology.py` の `test_dim_descriptions_start_with_the_label`
  - 確定前の課題: `backend/tests/design/test_reuse.py` の `test_dim_column_names_must_keep_the_ontology_word`
- 同じラベルと `same_as`: `backend/tests/ontology/test_ontology.py` の `test_same_as_names_the_canonical_attribute`、`test_duplicate_labels_fail_the_check`
- 設計の検査と CI の判定がそろっていること: 直接比べるテストは無い。両方が同じ関数（`Ontology.word_mismatch`・`Ontology.expr_column`）を使うことで担保している。

## 追記
