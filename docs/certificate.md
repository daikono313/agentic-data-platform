# Certificate と Promotion policy

/goal で Claude が自律開発するときに使う「マージしてよいか」の判定基準。人の判断を挟まずに検証できる条件だけを並べる。

## Certificate（PR ごとの合格条件）

すべて満たしたら、その PR は certificate を得たものとする。

| # | 条件 | 確認方法 |
|---|---|---|
| C1 | lint と型チェックが通る | `make lint`（ruff / mypy / eslint / tsc） |
| C2 | ユニットテストが通る | `make test`（pytest / vitest） |
| C3 | 変更したコードにテストがある | PR の差分に、対応するテストの追加・更新が含まれる |
| C4 | サンプルデータで dbt が通る | `make dbt-build`（`dbt build` + `dbt parse`。指標があれば `mf validate-configs`）。指標と生成物の `mf query` は生成の統合テスト（`backend/tests/generation/`）で確認する |
| C5 | eval が通る（インタビューや生成に関わる変更のみ） | `make eval`（CI では録画した応答で実行し、ローカルでは実際の LLM で実行） |
| C6 | CI が green | GitHub Actions |
| C7 | 敵対的レビューで blocking な指摘がない | **新しいコンテキストの subagent** が、issue の完了条件・[design-doc.md](design-doc.md)・差分を読んでレビューする。指摘は PR コメントに残す |
| C8 | 秘密情報が含まれない | 差分に `.env` の値、トークン、鍵が含まれない（CI で検査する） |
| C9 | 設計と矛盾しない | Resolved issues の決定を覆す変更なら、ADR（`docs/adr/`）を追加し、design-doc を更新している |
| C10 | プロンプトの変更は eval の前後比較で効果を示す（インタビューのプロンプト・知識ファイル・tool の説明を変える PR のみ） | 変更の前と後で `make eval REPEAT=3` を回し、`make eval-compare BEFORE=… AFTER=…` の表を PR 本文に貼る。合格率が下がらず、狙った数値（フィードバックの分類に対応するもの）がよくなっていること。プロンプトのバージョン（`PROMPT_VERSION`）を上げる（[evals/README.md](../evals/README.md)） |

## Promotion policy（マージの扱い）

| Tier | 対象 | マージ |
|---|---|---|
| T1 | このリポジトリ内で完結する変更すべて | certificate を得たら Claude が squash merge してよい |
| T2 | **このディレクトリの外**での操作が必要なもの（グローバル設定、他のリポジトリ、システムへのインストールなど） | 実行前にオーナーに確認する |
| — | システム（このディレクトリの外）に対する変更・削除 | **行わない** |

## 失敗時の扱い

- 同じ種類の失敗が繰り返されたら、個別の変更を直すのではなく、ワークフロー（プロンプト、テンプレート、certificate）の側を直す。直した内容は PR にまとめる。
- 再試行の回数と原因は PR 本文に書く。

## MVP の完了（/goal の終了条件）

1. Umbrella [#18](https://github.com/daikono313/auto_data_platform/issues/18) の子 issue がすべて close されている
2. design-doc の S1 シナリオがローカルで通しで動く（実際の LLM を使用、#17）
3. main の CI が green
