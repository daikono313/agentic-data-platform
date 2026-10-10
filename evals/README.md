# evals/

インタビューと生成の品質を測るシナリオ。

- `interview/<scenario>/`: ユーザー役の応答スクリプトと、期待する BEAM 成果物（イベント・粒度・measure・ディメンション）。
- `generation/<scenario>/`: 入力の BEAM 成果物と、生成物の合格条件（`dbt build` が green、`mf query` の結果など）。
- CI では録画した LLM の応答で再生する。ローカルでは `make eval` で実際の LLM を呼ぶ（Claude Code の認証）。
- MVP では EC 売上の 3 シナリオを用意する（design-doc の S1、S2 + 1 つ）。

## フィードバックから改善するサイクル（#45）

1. **集計**: `make feedback-report`（`SUMMARY=1` で Claude による要約と改善案）。プロンプトのバージョン別・段階別・分類別の 👎 の割合と、👎 の発言の抜粋が出る（`.adp/reports/` に保存。会話の抜粋を含むので公開しない）。
2. **eval 化**: `make feedback-drafts` で、低評価のセッションから eval シナリオの下書きが `.adp/eval-drafts/` にできる。フィードバックの分類に対応するチェックが入っている。**request と persona を合成した内容に書き換えてから** `evals/interview/` に置く（実データの会話をリポジトリに入れない）。
3. **前後比較**: 変更前に `make eval REPEAT=3`、プロンプトを直して `PROMPT_VERSION` を上げ、もう一度 `make eval REPEAT=3`。`make eval-compare BEFORE=.adp/evals/<前> AFTER=.adp/evals/<後>` の表を PR に貼る（certificate C10）。

### 会話の質のチェック（どのシナリオでも行う）

| チェック | 内容 | 対応するフィードバックの分類 |
|---|---|---|
| `no_choice_position_words` | 「上の選択肢」など、画面の位置を指す言葉を使わない | 選択肢が合わない |
| `question_not_repeated_in_text` | ボタンの上の質問を本文にも書かない | 質問が重複している |
| `no_repeated_questions` | 別の発言で同じ質問をしない（類似度 0.95 以上） | 質問が重複している |
| `no_unexplained_jargon` | 「粒度」「ディメンション」などを、最初に使うときに説明する | 専門用語がわからない |

シナリオの `checks` に `max_assistant_turns: N`・`max_assistant_chars: N` を書くと、会話の長さの上限も確かめる（質問が長い・多い）。前後比較の表には、ターン数・質問の数・発言の平均文字数・説明なしの専門用語・重複した質問・位置を指す言葉の数が出る。
