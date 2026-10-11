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

## v2 の完了

1. Umbrella [#47](https://github.com/daikono313/auto_data_platform/issues/47) の子 issue がすべて close されている
2. design-doc の S3 と S4 の 1〜2 が通しで動く: 自動テスト（`backend/tests/test_e2e_v2.py`、CI）と、実際の LLM（`make e2e SCENARIO=return_rate_by_category V2=1`）
3. main の CI が green

## v3 の完了（/goal の終了条件）

終了条件は結果だけで判定する（進め方の順序は条件にしない）。

1. Umbrella [#82](https://github.com/daikono313/auto_data_platform/issues/82) 本文のチェックリストにある issue が、次のどちらかになっている
   - close されている
   - オーナーの判断待ちで open のまま: `needs-owner` ラベルが付き、issue に「理由」と「オーナーがやること（実行するコマンドや判断事項）」が書かれている。対象になりうるのは次のものだけ
     - #80: 本物の PR が 1 件残るため、実行してよいかをオーナーが決める
     - #81 のうち PII の扱いの方針の承認（接続先を差し替えられるようにする実装はエージェントが行う。実際の接続はしない）
     - #76 のうち、画面で集まった本物のフィードバックが無い部分（自分で作ったフィードバックで「1 周」したことにしない）
2. 新しい源泉（在庫 `raw.inventory` を #69 でサンプルデータに追加する）を足して「オントロジーの更新 → クレンジング層 → fact / dim → 指標 → チャート（画像が生成される）」が通しで動く
   - 自動テスト: `backend/tests/test_e2e_v3.py`（CI。LLM はモック）
   - 実際の LLM: `make e2e SCENARIO=inventory V3=1`（ローカル。#69 で追加）
3. main の CI が green

1〜3 を満たしたら /goal を終了し、`needs-owner` の issue の一覧をオーナーに報告する。

補足:
- #74 の自動反映は GitHub Actions かアプリ内の仕組みで作る（launchd / cron などシステムの変更はしない）
- #79 は issue 本文の 3 項目を入れたら close する
