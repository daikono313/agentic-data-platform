# 0003: 描画画面は MetricFlow と dbt Charts を backend のプロセス内で使う（O5）

- 日付: 2026-10-11
- ステータス: Accepted
- 関連: #43、ADR 0001（の 3 を一部更新）、ADR 0002、design-doc の O5

## 背景

描画画面（#43）では、指標の切り口・粒度・期間を変えるたびにチャートを描き直す。最初は ADR 0001 の方針どおり CLI をサブプロセスで呼ぶつもりだった（`mf query --explain` で SQL を作り、`dct render` で描く）。しかし計測すると、1 回の描き直しに約 20 秒かかった（`mf` が 7〜10 秒、`dct render` が 7〜14 秒）。時間のほとんどは、起動のたびの import（dbt アダプタなど）と設定の読み込みだった。

## 決定

- **SQL の作成**: backend のプロセス内で MetricFlow を使う（`adp.charts.MetricFlowCompiler`）。
  - `target/semantic_manifest.json` だけを読み、SQL を実行しないクライアントを渡す。DB には接続しない。dbt アダプタ経由（`mf` CLI の組み立て）だと DuckDB を書き込み用に開いたままになり、`dbt build` を妨げるため。
  - 出力は `mf query --explain` と同じ（テストで確認）。manifest が更新されたら読み直す。
  - データ一覧（#38）の切り口（`mf list dimensions`）も同じものから取る。
- **描画**: dbt Charts の公開 API（`dbt_charts.agent_api.ProjectSession.render_board`）をプロセス内で使い、メモリ上のボード（`InMemoryBoard`）を SVG に描く（`adp.charts.BoardRenderer`）。
  - 接続は warehouse の `dbt_charts.yml`（dbt プロファイル経由の `warehouse` だけ。描画の前に検査する）。DuckDB の外部アクセス禁止が効く。
  - 描画のたびに接続を閉じる（DuckDB を開いたままにしない）。
- **画面での表示（O5）**: backend が SVG を返し、frontend は `<img src="data:image/svg+xml,...">` で表示する（`<img>` の SVG はスクリプトを実行しない）。`dct serve` は使わない。
- **入力**: 画面から受け取るのは指標名・切り口名・粒度・期間だけ。指標と切り口は Semantic Layer の一覧と照らし合わせ、ボードは生成時と同じ許可リスト（ADR 0002）を通す。SQL は MetricFlow が作ったものだけ。
- **生成 PR のプレビュー**: 生成ジョブの worktree（モデルを build 済み）を同じ仕組みで描く。マージ前に「確定した指標をその場で見る」（#43 の完了条件）。worktree の場所はジョブの記録（ブランチ名）から backend が決める。

結果: 2 回目以降の描き直しは、SQL の作成が数十ミリ秒、描画が約 0.15 秒（初回はそれぞれ 1〜2.5 秒）。

## 影響

- ADR 0001 の 3（「backend からは `mf` CLI をサブプロセスで呼ぶ」）を、描画画面とデータ一覧については更新する。生成パイプライン・`adp.charts compile / check`・検証は、これまでどおり CLI を使う。
- MetricFlow（0.213）と dbt Charts（0.9.x）の Python API に依存する。dbt Charts の `agent_api` は公開 API だが、MetricFlow の `MetricFlowEngine` は内部 API に近い。バージョンを上げるときは `test_chart_view.py`・`test_datasets.py`・`test_charts.py` で確かめる（`test_chart_view.py` に、CLI と同じ SQL になることのテストがある）。
- プロセス内の dbt Charts は、環境変数 `DBT_PROFILES_DIR` / `ADP_DUCKDB_PATH` でプロファイルや DB の場所が変わる（CLI のように固定できない）。設定されていれば描画を止めて理由を返す。
- 切り口の値が多いと SVG が十数 MB になるので、3 MB を超えたら描かずに、粒度・期間・切り口を変えるよう返す。

## 検討した代替案

- **CLI のサブプロセス**: 実装は単純だが、1 回 20 秒では切り口を試しながら見られない。
- **結果データを返して React で描く（recharts など）**: 速いが、チャートの描き方が dbt Charts（生成 PR のチャート）と別になり、依存も増える。
- **`dct serve` を 127.0.0.1 で起動して backend が中継**: 起動済みなので速いが、Host を検証しないサーバーをローカルで常に動かすことになる（DNS リバインディングで任意の SQL を実行される）。
