# フィードバックから改善する手順（#45 / #76）

画面で集まった評価から、インタビューのプロンプトを直す一周の手順。会話には実データが入るので、
**集計・下書きはローカルだけで扱い、リポジトリや PR には合成した内容しか入れない**（design-doc のプライバシー）。

## 1. 集める

- インタビューの発言ごとに 👍 / 👎（👎 は分類とコメント）、終わったらセッション全体を 5 段階で評価してもらう。
- 分類（重複・専門用語・長い・選択肢の位置の言葉など）は eval のチェックに対応している
  （`adp.evals.feedback.CATEGORY_CHECKS`）。分類を付けてもらうと、下書きのチェックが自動で入る。

## 2. 集計する

```
make feedback-report            # .adp/reports/feedback-<日時>.md に保存
make feedback-report SUMMARY=1  # Claude による要約と改善案も（フィードバックが無ければ省く）
```

- 👎 の発言と、低評価（2 点以下）のセッションを見る。
- 👍 のコメントと高評価（4 点以上）のコメントは「直すときに壊さない点」として控える。

## 3. eval にする

```
make feedback-drafts            # .adp/eval-drafts/feedback_<session>.yml
```

- 下書きは実際の会話を元にしている。`request` と `persona` を**合成した内容に書き換えて**から
  `evals/interview/` に置く。分類が無い 👎 は、会話を見て `checks` を決める。
- 0 件のときは、何をすれば下書きができるかを表示する（低評価がまだ無い）。

## 4. 直して比べる

1. 直す前に `make eval REPEAT=3`（結果は `.adp/evals/` に残る）
2. プロンプトを直し、`PROMPT_VERSION` を上げる
3. 直した後に `make eval REPEAT=3`
4. `make eval-compare BEFORE=… AFTER=…` の結果を PR に貼る（certificate C10）

## 5. 確かめる

- マージ後、画面で集まる評価がプロンプトのバージョン別にどう変わったかを `make feedback-report` の
  「プロンプトのバージョン別」で見る。
