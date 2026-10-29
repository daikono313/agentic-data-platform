# 0009: 属性の履歴は、dbt snapshot と別の履歴モデル（SCD Type 2）で持つ

- 日付: 2026-10-22
- ステータス: Accepted（2026-10-23、オーナー（Claude Code のセッションで回答））
- 関連: [#141](https://github.com/daikono313/agentic-data-platform/issues/141)、design-doc の R6・R15、ADR [0006](0006-make-ontology-labels-the-business-terms.md)・[0007](0007-map-each-concept-to-one-model.md)・[0008](0008-generate-state-snapshots-from-beam.md)
- 見直し: MetricFlow が、期間付きのモデルに primary の entity を置けるようになったとき。または、役割付きのディメンション（配送先顧客など）の履歴が要る要求が来たとき

## 背景

design-doc の R6 は「SCD は Type1 のみ実装する（履歴の要否は記録する）」。BEAM の属性の `history_required: true`（値が変わったとき、過去の分析を当時の値で見たい）は、記録するだけで、生成では WARNING を出して上書きしている（`backend/src/adp/design/builder.py`）。

v5（オーナーの指示、2026-10-12、#141）で、履歴の要る属性を、当時の値で集計できるようにする。

この ADR の「snapshot」は dbt snapshot（源泉の行の版を記録する仕組み）のこと。ADR 0008 の「定期スナップショット」（状態の fct）とは別物。

2026-10-22 に、`warehouse/` の複製で確かめたこと。版は dbt-core 1.12.5、dbt-duckdb 1.11.0、metricflow 0.213.0。

- **dbt snapshot（YAML）で履歴を取れる。** `strategy: timestamp` で `dbt_valid_from` / `dbt_valid_to` の版が残る。源泉の値を書き換えて snapshot を 2 回実行すると、版が 2 つになった。
- **MetricFlow（latest spec）で期間の結合ができる。** モデルの YAML に、entity の種類 `natural` と、時間のディメンションの `validity_params`（`is_start` / `is_end`）を書く。fct の外部 entity は、agg_time の日付がその期間に入る版に結合された（`mf query --explain` で確かめた）。
- **MetricFlow の制約が 3 つある。**
  - 期間付きのモデルには、primary / unique の entity を置けない（`dbt parse` が「not yet supported」で止まる）。
  - 1 つのモデルに置ける natural の entity は 1 つまで。そのため、役割付きのディメンションの履歴は 1 つのモデルで表せない。
  - 現在の値の dim と同じディメンション名にすると、`mf validate-configs` は通るのに、両方とも問い合わせられなくなる（「multiple matching join paths」）。
- **履歴は snapshot を始めた後からしか取れない。** 源泉は上書きされているので、どちらの方式でも同じ。違うのは変更の時刻の記録だけで、`strategy: timestamp` は源泉の更新日時、`strategy: check` は snapshot を実行した時刻になる。実行の間に何度か変わったときは、最後の値だけが残る。
- **源泉の更新日時が無い表がある。** stores・products・promotions は更新日時の列を持たないので、`strategy: check` になる（`warehouse/scripts/generate_sample_data.py` の DDL で確かめた）。

## 判断の基準

- **既存の決定と合うこと。**
  - 既存の dim は再利用し、列の追加だけを許す（R15）。
  - 概念とモデルは 1 対 1（ADR 0007）。
  - 属性のラベルはオントロジーの言葉にそろえる（ADR 0006）。
- **既存のモデル・チャートを壊さないこと。**
- **決まった規則で検査できること。**
- **今の MetricFlow で動くこと。**

## 検討した選択肢

### 案 A: snapshot と、別の履歴モデル `dim_<name>_history` を作る（採用）

- 履歴の要る属性を持つディメンションごとに、snapshot（`warehouse/` の `snapshots` に置く `snp_<table>.yml`）と、そこから作る SCD2 のモデル `dim_<name>_history` を生成する。
- 履歴モデルの中身:
  - 版のキー
  - 現在の値の dim と同じキー（`<name>_key`）。natural の entity の名前は、現在の値の dim の primary の entity と同じ `<name>` にする。だから fct を変えずに結合できる
  - 履歴の属性
  - 期間（`valid_from` / `valid_to`）
- 最初の版は、暦の始まりまで期間を広げる。広げないと、源泉の更新日時より前の出来事が結合から外れる。
- MetricFlow のディメンション名は `<属性>_at_event`（当時の値）にし、現在の値の dim と重ならないようにする（entity の名前が同じなので、名前が重なると問い合わせられなくなる）。ラベルはオントロジーの属性のラベルに「（当時）」を付ける（ADR 0006 の言葉の正本はそのまま。列はオントロジーの列）。
- entity の名前を別にして（例: `<name>_history`）fct に外部の entity を足す方法もあるが、既存の fct を変えることになる（R15）ので採らない。
- 良い点:
  - 既存の dim と fct を変えない（R15）。
  - 同じディメンションの外部キーを持つすべての fct で、当時の値で切れる。
  - 今の MetricFlow で動くことを確かめた。
- 悪い点:
  - 1 つの概念に 2 つのモデル（現在の値の dim と履歴モデル）が結び付く。ADR 0007 の 1 対 1 の例外になるので、オントロジーで履歴モデルを概念の従属として明示する。
  - 役割付きのディメンションの履歴は作れない（確定前の課題にする）。
  - snapshot を始める前の出来事は、始めた時点の値で集計する（当時の値ではない）。

### 案 B: dim_<name> 自体を SCD2 にする（版のキーを持たせる）

- 良い点: モデルが増えない。1 対 1 を保てる。
- 悪い点:
  - 期間付きのモデルに primary の entity を置けない（確かめた）。そのため、役割の entity（unique）や外部キーの検査が崩れる。
  - 既存の dim とすべてのチャートが変わる（R15 に反する）。そのため採らない。

### 案 C: fct に時点の版のキーを持たせる

- 良い点: MetricFlow は普通の primary / foreign の結合で済む。役割付きにも対応できる。
- 悪い点:
  - 規約「fct の外部キーは、dim と結合せずに自然キーから作る」（結合の漏れで行が消えるのを防ぐ）に反する。
  - 既存の fct を作り直すことになり、snapshot が変わるたびに fct の作り直しが要る。そのため採らない。
  - MetricFlow の制約が解けない場合の代わりとして残す。

## 決定

案 A を採る。判断の基準の「既存のモデル・チャートを壊さないこと」と「今の MetricFlow で動くこと」を優先した。ADR 0007 の 1 対 1 は「現在の値の dim」で保ち、履歴モデルは概念の従属として明示する。


## 結果

- 良い影響:
  - 顧客の地方のような属性を、出来事の時点の値で集計できる。
  - 既存の dim・fct・チャートは変わらない。
- 悪い影響・トレードオフ:
  - 1 つの概念に、従属の履歴モデルが付く（オントロジーとその検査を直す）。
  - 役割付きのディメンションと、結合した表から来る属性の履歴は作れない。
  - snapshot を始める前の変更は復元できない。開始前の出来事は、開始時点の値で集計する。
  - 同じディメンション名の重なりは MetricFlow の検証で見つからないので、規約の検査で止める必要がある。
- 後続の作業（#141）:
  - BEAM の `history_required` を変化の種類（今の値だけ・履歴あり・固定）に広げるかは、別に決める（#141 の本文。インタビューで聞くことが変わるので C10）。
  - サンプルデータの生成（`make setup`）で、snapshot の表も消す。
  - 履歴を確かめる eval・e2e のために、サンプルデータに属性の変更を起こす方法。
  - オントロジーの概念の従属モデル（`history_model`）と、検査で snapshot のノードもたどること。
  - 生成器: snapshot・履歴モデル・unit test・規約の検査（版のキー、期間の重なり、不明メンバー、名前の重なり）。
  - modeling-conventions の「SCD」の節（T3）。
  - インタビューで聞くこと（C10）。

## 守らせ方

- 今は無い。後続の PR までは、生成器は Type1 で作り、警告を出す（`backend/src/adp/design/builder.py`）。後続の PR で足す検査は、期間の重なり（`dbt_utils.mutually_exclusive_ranges`）と、ディメンション名の重なり（`adp.generation.conventions`）。

## 追記

- 2026-10-26: 後続の PR（#224）で生成器・検査を実装した。「守らせ方」の「今は無い」は古くなった。今の守らせ方は、生成器（`adp.generation.dbt.history_files`。snapshot・履歴モデル・semantic model）、生成するテスト（`dbt_utils.mutually_exclusive_ranges` で版の隙間・重なり）、`make dbt-build` の規約の検査（`adp.generation.conventions`。今の値の dim と同じ entity、ディメンション名の重なり、期間のテスト、各キーの版が暦の全体を覆うこと、不明メンバー）と、オントロジーの照合（概念の従属の `history_model` が snapshot を経由して源泉を読むこと）。規約は modeling-conventions の「SCD」（#225）。
- 2026-10-26: 実装で分かったこと。案 A の「最初の版は暦の始まりまで広げる」は、`mutually_exclusive_ranges` では確かめられない（版どうしの隙間・重なりしか見ない）ので、規約の検査で止める。暦の範囲は、生成物の Jinja の許可リストを広げずに `dim_date` から取る。
- 2026-10-29: 後続の作業の「履歴を確かめる eval・e2e のために、サンプルデータに属性の変更を起こす方法」を実装した。サンプルデータの生成（`warehouse/scripts/generate_sample_data.py`）は、顧客の一部に、2024-07-01 から期間の終わりまでの引っ越し（都道府県・地方）と会員ランクの変更を入れる（今の値は変えず、過去の版を作る）。`make sample-data`（`--dbt`）は、2024-07-01・2025-01-01・2025-07-01 の時点の源泉を順に書いて、そのたびに dbt で snapshot を取り、最後に今の源泉を書く（次の `make dbt-build` の snapshot で今の版が足される）。生成した版がすべて snapshot に残るよう、登録や別の変更と同じ時点の間に入る変更は起こさない（起こすと、時点の間の最後の値だけが残り、最初の版が抜けた顧客は、履歴モデルが後の版を暦の始まりまで広げて当時の値を誤る）。snapshot は実行した時点の源泉しか見ないので、運用で定期的に snapshot を取ったときと同じ版が残る。`strategy: check` の表（stores・products）は変更を入れていない（版の時刻が snapshot を実行した時刻になり、当時の値を確かめられないため）。
