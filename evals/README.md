# evals/

インタビューと生成の品質を測るシナリオ。

- `interview/<scenario>/`: ユーザー役の応答スクリプトと、期待する BEAM 成果物（イベント・粒度・measure・ディメンション）。
- `generation/<scenario>/`: 入力の BEAM 成果物と、生成物の合格条件（`dbt build` が green、`mf query` の結果など）。
- CI では録画した LLM の応答で再生する。ローカルでは `make eval` で実際の LLM を呼ぶ（Claude Code の認証）。
- MVP では EC 売上の 3 シナリオを用意する（design-doc の S1、S2 + 1 つ）。
