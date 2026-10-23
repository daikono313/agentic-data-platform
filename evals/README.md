# evals/

インタビューと生成の品質を測るシナリオ。

- `interview/<scenario>.yml`: ユーザー役（ペルソナ）と最初の要求、合格条件（`checks`。確定できるか・指標の集計方法・切り口・会話の質など）。`onboard: <源泉>` を書いたシナリオ（reviews）は、まだオントロジーに無い源泉を使う。`make eval` ではプロファイルからの決定的な追加案で取り込んだものとして設計を確かめ、`make e2e V3=1`（既定のシナリオが reviews）では本物の取り込み（エージェント → PR）をしてから生成する。
- `repair/<name>.yml`: 生成物にわざと入れる失敗と、期待する結果（下の「生成の自己修復の eval」）。
- 生成の合格条件（`dbt build` が green、`mf query` の結果など）は、シナリオではなく生成の統合テスト（`backend/tests/generation/`）で確かめる。
- CI では録画した LLM の応答（`backend/tests/fixtures/evals/`）で再生する。ローカルでは `make eval` で実際の LLM を呼ぶ（Claude Code の認証）。
- **シナリオは人間が確かめて足す**（会話はすべて合成。実データの会話を入れない）。フィードバックからプログラムが下書きを作ったときも、人間が合成した内容に書き換えてから置く。既存のシナリオは開発時に Claude Code が書いた。チェックの実装（`backend/src/adp/evals/`）はコードなので AI が書く。

## フィードバックから改善するサイクル（#45）

集計（`make feedback-report`）→ eval 化（`make feedback-drafts`）→ 前後比較（`make eval-compare`）の手順は [docs/feedback-loop.md](../docs/feedback-loop.md) にまとめている。前後比較の注意:

- 前と後は**同じ eval のコード**で回す（チェックや数値の定義が変わると比べられない）。
- 全体の行は前後の両方にあるシナリオだけで集計する。

### BEAM 成果物のチェック（シナリオの `checks` に書く）

| チェック | 内容 |
|---|---|
| `finalizable` | 確定できた（確定までの課題が無い。設計の課題を含む） |
| `has_w: [what, when, …]` | 出来事に、その W のディメンションがある |
| `metric_aggregation: <sum など>` | 指標の measure が、その集計方法でデータがある |
| `metric_type: <simple など>` | 指標の種類 |
| `metric_purpose` | どれか 1 つの指標に、聞いた目的（purpose）が記録されている（#159） |
| `has_gap: <種類>` / `min_events: N` | 足りないもの・出来事の数 |
| `fact: <fct の名前>` | 出来事の fact がその名前になる（既存の fact を再利用したか。#113） |
| `story_type: <discrete / recurring / evolving>` | 指標の出来事が、その種類（出来事・定期的な状態・進んでいく状態）になっている（#135） |
| `grain_keys_w: [what, where, …]` | 指標の出来事の粒度のキー（grain_keys）に、その W のディメンションがすべてある（縮退ディメンションは W を持たないので数えない） |
| `measure_additivity: <additive など>` | 指標の measure が、その加法性（足し上げてよいか）になっている |
| `min_examples: N` | 指標の出来事それぞれに、具体例が N 件以上ある |
| `example_kinds: N` | 指標の出来事それぞれに、型（kind。典型・別パターン・繰り返し・欠損・グループ）の違う具体例が N 種類以上ある（#142） |
| `has_hierarchy[: <ディメンション>]` | 階層のあるディメンションがある（名前を書けば、そのディメンションに） |
| `shared_dimensions: [what, when, …]` | その W ごとに、2 つ以上の出来事が同じ名前のディメンションを持つ（共通ディメンション）。When は名前によらず dim_date で共通なので、2 つ以上の出来事にあればよい |
| `fact_source: <源泉>` | その源泉を 1 行の元（設計と同じく、粒度のキーから決まるベースのテーブル。`adp.design.builder.base_table`）にした出来事があり、その fact がほかの源泉の既存の fact（オントロジーの概念のモデル）ではない。fact の名前は問わない（データ全体にしかない要求で新しい fact を作るか。#113） |
| `metric_group_by_columns: [raw.orders.ordered_at, [raw.stores.store_name, raw.stores.store_id], …]` | 指標の切り口に、その元の列がすべてある（属性は元の列、When はイベント側の日時の列に解決する。ディメンションや属性の名前の揺れによらない。要素をリストにすると、そのどれか 1 つ。#155） |
| `metric_filter: {column: <元の列>, excludes: [...], includes: [...]}` | どれか 1 つの指標の絞り込みが、その列の excludes（必須）の値をすべて除き、includes の値をすべて残す（例: キャンセルを除く、入荷だけに絞る。#155） |
| `ratio_of: {numerator: <元の列>, denominator: <元の列>}` | 比率の指標の分子・分母の measure が、その元の列の集計になっている（例: 返品数量 ÷ 販売数量。#155） |

元の列で採点するチェックは、源泉が式（`expr`）の measure・When を列として数えない（式の中の列とは一致しない）。

### 会話の質のチェック（どのシナリオでも行う）

| チェック | 内容 | 対応するフィードバックの分類 |
|---|---|---|
| `no_choice_position_words` | 「上の質問にお答えください」「上の選択肢から」「ボタンから選んで」など、画面の位置や操作を指す言い方をしない。こうした文はアプリが本文から取り除く（`adp.interview.wording`）が、取り除いた文（`removed`）もモデルの振る舞いとして数える | 選択肢が合わない |
| `question_not_repeated_in_text` | ボタンの上の質問を本文にも書かない | 質問が重複している |
| `no_repeated_questions` | 別の発言で同じ質問をしない（類似度 0.95 以上。ユーザーの聞き返しの直後と、確定の確認は除く） | 質問が重複している |
| `no_unexplained_jargon` | 「粒度」「ディメンション」などを、最初に使うときに説明する（括弧・「とは」で前後に説明があればよい） | 専門用語がわからない |
| `no_effort_question` | 工数（作る手間）の見積もりをユーザーに聞かない（モデラーが見積もる。#76）。「…」の引用は除く | （画面のコメント） |
| `examples_shown` | 具体例を記録してよいかを確かめるときに、値（日付・数量・金額・ID）を発言に書く。tool で見た行はユーザーに見えない（#163）。直前の発言で示していればよい | 選択肢が合わない |
| `reframe_shown` | 言い換えの確認（「この言い換えで合っていますか？」）では、言い換えた問い（「月ごとの売上金額はいくらか」など）を発言に書く。記録した内容（upsert_metric）はユーザーに見えない（#165）。直前の発言で示していればよい（アシスタントが書いた文だけを見る。ユーザーの要求文や、確認の後ろの別の質問では通さない）。判定は `present_choices` のエラーと同じ（`adp.interview.wording.hidden_confirmation`） | 何を聞かれているかわからない |

シナリオに書いたときだけ行うチェック: `asks_irregular_cases`（典型の具体例の後に、取消・マイナス・二重記録など、典型と違うケースを聞いたか。#76・#142）、`asks_purpose`（要求の目的、つまり指標を見て何を判断・行動するかを聞いたか。#135。7W の Why / How のディメンションは `has_w` で確かめる）。

シナリオの `checks` に `max_assistant_turns: N`・`max_assistant_chars: N` を書くと、会話の長さの上限も確かめる（質問が長い・多い）。前後比較の表には、ターン数・質問の数・発言の平均文字数・説明なしの専門用語・重複した質問・位置を指す言葉・締めの一言・二重の本文・工数を聞いた質問の数が出る。締めの一言は、質問を出した発言の「回答をお待ちしています」「選択肢から選ぶか、自由に書いてください」などの文で、アプリが本文から取り除く（`adp.interview.wording`）が、モデルの振る舞いとして数える（#158）。二重の本文は、画面に出た本文に同じ地の文（8 文字以上。全角・半角や句読点の違いは無視）が 2 回ある発言の数で、tool 呼び出しをまたいでモデルが同じ文を書き直したもの（アプリは消さない。言い回しを変えた文やリストの行の二重は数えない。#161）。発言の文字数・質問の数は画面に出た文で数えるので、#158 より前の結果と比べると、取り除いた分だけ短く見える（モデルの変化ではない）。

## 生成の自己修復の eval（#78）

生成で実際に起きた失敗の型を `evals/repair/<name>.yml` に書き、生成した dbt プロジェクトにわざと入れて、
自己修復（`adp.generation.repair.Repairer`）が直せるかを実際の LLM で測る。

```
make eval-repair                      # 全シナリオ
make eval-repair SCENARIO=sql_typo REPEAT=3
```

- シナリオ: `beam`（生成に使う BEAM 成果物）、`fault`（`file` と `replace: [old, new]` か `delete_lines_with`）、
  `expect`（`fixed` か `handed_to_human`）
- 結果: `.adp/evals/repair-<日時>/`（シナリオごとの JSON と `summary.md`。期待どおりの割合・修復の試行回数・時間）
- 新しい失敗の型が出たら、シナリオを足す。生成器が変わって失敗が入らなくなったシナリオは、CI
  （`tests/evals/test_repair_eval.py`）で気づく
- 自己修復のプロンプト・テンプレートを変える PR は、前後で `make eval-repair REPEAT=3` を回して表を貼る
