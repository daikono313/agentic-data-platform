# agentic-data-platform

ビジネスユーザーが「xxx の売上指標を取りたい」と入力すると、Claude が BEAM✲ に沿ってヒアリングし、dbt（dim / fct）・dbt Semantic Layer・テスト・チャートを実装した PR を自動で作る、AI 駆動のデータ基盤・モデリング開発の Web アプリ。新しい源泉は、オントロジー → クレンジング層 → fact / dim の順に取り込む。

- 設計（全体のフローと担当）: [docs/design-doc.md](docs/design-doc.md#全体のフローと担当)
- 合格条件: [docs/certificate.md](docs/certificate.md)
- 進捗: Umbrella issue [#18](https://github.com/daikono313/agentic-data-platform/issues/18)（MVP）・[#47](https://github.com/daikono313/agentic-data-platform/issues/47)（v2）・[#82](https://github.com/daikono313/agentic-data-platform/issues/82)（v3）

構成: FastAPI + React / DuckDB + dbt + MetricFlow + dbt Charts / Claude Agent SDK

## 人間と AI の担当

| 人間 | AI（Claude） | 決定的なプログラム |
|---|---|---|
| 要求の入力、インタビューへの回答、確定 | インタビュー（質問・BEAM 成果物） | スター設計、dbt・Semantic Layer・テストの生成 |
| 源泉の取り込み案の確認・修正 | 取り込みの案（オントロジーへの追加） | クレンジング層（stg）・オントロジーの図の生成 |
| PR のレビューとマージ | 検証に失敗した生成物の修復 | チャートの定義と画像、PR 本文、マージ後の同期 |
| フィードバック、改善の方針 | アプリのコードとプロンプト（Claude Code） | フィードバックの集計 |
| ドキュメント（方針・規約・知識） | — | — |

原則は「コードは AI が書き、ドキュメントは人間が書いて直す」。ファイルごとの持ち主は [design-doc の「ファイルの持ち主」](docs/design-doc.md#ファイルの持ち主) を参照。

## セットアップ

前提: [uv](https://docs.astral.sh/uv/)、pnpm 9、Node.js 22、Python 3.12（`uv python install 3.12`）

```
make setup      # 依存のインストールとサンプルデータ
make lint test  # lint / 型チェック / テスト
make dbt-build  # dbt build と MetricFlow・チャート・オントロジーの検査
make dev        # backend (:8000) と frontend (:5173) を起動
```

LLM はローカルでは Claude Code のログイン認証で動きます。API キーで動かす場合は `.env.example` をコピーして `.env` に `ANTHROPIC_API_KEY` を設定してください。

## 使い方

1. `make setup` のあと `make dbt-build`（dbt のメタデータを作る。ソースカタログが使う）
2. `make dev` で起動し、http://localhost:5173 を開く（`?demo` でダミーデータの画面を見られる）
3. 開始画面で、既存の指標・fact × dimension のバスマトリクス・使える源泉を見る。✓ からチャートを開き、空欄から要求を書ける
4. 見たい指標を入力すると、Claude が BEAM✲ に沿って質問する。答えていくと BEAM Canvas と Event Matrix が埋まる
5. 確定すると、生成ジョブが `.adp/worktrees/` で dbt・Semantic Layer・テスト・チャートを生成して検証し（失敗すれば自己修復）、GitHub に PR を作る
6. PR をマージしたら、開始画面の「取り込んで更新する」で手元に反映する
7. 新しい源泉は、源泉の一覧の「取り込む」から、オントロジーへの追加案とクレンジングの定義を確かめて PR にする

PR は origin の所有者のアカウント（`gh auth token -u <owner>`）で作る。別のアカウントにする場合は `.env` に `ADP_GITHUB_ACCOUNT` を書く。

## 品質の確認

```
make eval REPEAT=3                                  # インタビューの eval（実際の LLM、5 シナリオ）
make eval-compare BEFORE=… AFTER=…                  # プロンプトを変えたときの前後比較
make eval-repair                                    # 生成の自己修復の eval
make e2e                                            # S1 を通しで実行（dry-run。PR=1 で本物の PR）
make e2e SCENARIO=return_rate_by_category V2=1      # S3・S4
make e2e SCENARIO=inventory V3=1                    # S5（源泉の取り込みから）
make feedback-report                                # フィードバックの集計（docs/feedback-loop.md）
```

CI では LLM を呼ばず、記録した実際のインタビュー（`backend/tests/fixtures/evals/`）を再生して確認する。

## 安全のための制限

[docs/guardrails.md](docs/guardrails.md) を参照（認証、エージェントの操作範囲、DuckDB の外部アクセス禁止、個人情報の列の値を出さない、PR 本文の無害化など）。
