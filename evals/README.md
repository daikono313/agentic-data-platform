# evals/

インタビューと生成の品質を測るシナリオ。

- `interview/<scenario>.yml`: ユーザー役（ペルソナ）と最初の要求、合格条件（`checks`。確定できるか・指標の集計方法・切り口・会話の質など）。`onboard: <源泉>` を書いたシナリオ（reviews）は、まだオントロジーに無い源泉を使う。`make eval` ではプロファイルからの決定的な追加案で取り込んだものとして設計を確かめ、`make e2e V3=1`（既定のシナリオが reviews）では本物の取り込み（エージェント → PR）をしてから生成する。
- `repair/<name>.yml`: 生成物にわざと入れる失敗と、期待する結果（下の「生成の自己修復の eval」）。
- 生成の合格条件（`dbt build` が green、`mf query` の結果など）は、シナリオではなく生成の統合テスト（`backend/tests/generation/`）で確かめる。
- CI では録画した LLM の応答（`backend/tests/fixtures/evals/`）で再生する。ローカルでは `make eval` で実際の LLM を呼ぶ（Claude Code の認証）。
- **シナリオは人間が確かめて足す**（会話はすべて合成。実データの会話を入れない）。フィードバックからプログラムが下書きを作ったときも、人間が合成した内容に書き換えてから置く。既存のシナリオは開発時に Claude Code が書いた。チェックの実装（`backend/src/adp/evals/`）はコードなので AI が書く。

## フィードバックから改善するサイクル（#45）

1. **集計**: `make feedback-report`（`SUMMARY=1` で Claude による要約と改善案）。プロンプトのバージョン別・段階別・分類別の 👎 の割合と、👎 の発言の抜粋が出る（`.adp/reports/` に保存。会話の抜粋を含むので公開しない）。
2. **eval 化**: `make feedback-drafts` で、低評価のセッションから eval シナリオの下書きが `.adp/eval-drafts/` にできる。フィードバックの分類に対応するチェックが入っている。**request と persona を合成した内容に書き換えてから** `evals/interview/` に置く（実データの会話をリポジトリに入れない）。
3. **前後比較**: 変更前に `make eval REPEAT=3`、プロンプトを直して `PROMPT_VERSION` を上げ、もう一度 `make eval REPEAT=3`。`make eval-compare BEFORE=.adp/evals/<前> AFTER=.adp/evals/<後>` の表を PR に貼る（certificate C10）。前と後は**同じ eval のコード**で回す（チェックや数値の定義が変わると比べられない）。全体の行は前後の両方にあるシナリオだけで集計する。

### BEAM 成果物のチェック（シナリオの `checks` に書く）

| チェック | 内容 |
|---|---|
| `finalizable` | 確定できた（確定までの課題が無い。設計の課題を含む） |
| `has_w: [what, when, …]` | 出来事に、その W のディメンションがある |
| `metric_group_by_w: <w>` | 指標の切り口に、その W のディメンションがある |
| `metric_aggregation: <sum など>` | 指標の measure が、その集計方法でデータがある |
| `metric_type: <simple など>` / `metric_has_filter` | 指標の種類・条件 |
| `has_gap: <種類>` / `has_examples` / `min_events: N` | 足りないもの・具体例・出来事の数 |
| `fact: <fct の名前>` | 出来事の fact がその名前になる（既存の fact を再利用したか。#113） |
| `fact_source: <源泉>` | その源泉を 1 行の元（設計と同じく、粒度のキーから決まるベースのテーブル。`adp.design.builder.base_table`）にした出来事があり、その fact がほかの源泉の既存の fact（オントロジーの概念のモデル）ではない。fact の名前は問わない（データ全体にしかない要求で新しい fact を作るか。#113） |

### 会話の質のチェック（どのシナリオでも行う）

| チェック | 内容 | 対応するフィードバックの分類 |
|---|---|---|
| `no_choice_position_words` | 「上の質問にお答えください」「上の選択肢から」「ボタンから選んで」など、画面の位置や操作を指す言い方をしない。こうした文はアプリが本文から取り除く（`adp.interview.wording`）が、取り除いた文（`removed`）もモデルの振る舞いとして数える | 選択肢が合わない |
| `question_not_repeated_in_text` | ボタンの上の質問を本文にも書かない | 質問が重複している |
| `no_repeated_questions` | 別の発言で同じ質問をしない（類似度 0.95 以上。ユーザーの聞き返しの直後と、確定の確認は除く） | 質問が重複している |
| `no_unexplained_jargon` | 「粒度」「ディメンション」などを、最初に使うときに説明する（括弧・「とは」で前後に説明があればよい） | 専門用語がわからない |
| `no_effort_question` | 工数（作る手間）の見積もりをユーザーに聞かない（モデラーが見積もる。#76）。「…」の引用は除く | （画面のコメント） |

シナリオに書いたときだけ行うチェック: `asks_irregular_cases`（具体例の後に、取消・マイナス・二重記録などイレギュラーなケースを聞いたか。#76）。

シナリオの `checks` に `max_assistant_turns: N`・`max_assistant_chars: N` を書くと、会話の長さの上限も確かめる（質問が長い・多い）。前後比較の表には、ターン数・質問の数・発言の平均文字数・説明なしの専門用語・重複した質問・位置を指す言葉・工数を聞いた質問の数が出る。

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
