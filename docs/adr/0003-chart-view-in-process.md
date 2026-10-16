# 0003: 描画画面は MetricFlow と dbt Charts を backend のプロセス内で使う（O5）

- 日付: 2026-10-11
- ステータス: Accepted
- 関連: #43、ADR 0001（の決定 3 を一部変更）、ADR 0002、design-doc の O5・O8（→ R20・R23）
- 見直し: MetricFlow か dbt Charts のバージョンを上げるとき（Python API への依存を確かめる）

## 背景

描画画面（#43）では、指標の切り口・粒度・期間を変えるたびにチャートを描き直す。最初は ADR 0001 の方針どおり CLI をサブプロセスで呼ぶつもりだった（`mf query --explain` で SQL を作り、`dct render` で描く）。しかし計測すると、1 回の描き直しに約 20 秒かかった（`mf` が 7〜10 秒、`dct render` が 7〜14 秒。2026-10-11）。時間のほとんどは、起動のたびの import（dbt アダプタなど）と設定の読み込みだった。

画面での表示方法（O5）も決める必要があった。`dct serve` は Host を検証しないので使えない（ADR 0002）。

## 判断の基準

- 切り口を試しながら見られる速さ（描き直しが 1 秒前後）
- 生成 PR のチャートと同じ描き方（dbt Charts）にする
- 新しい信頼の境界を増やさない（認証の無いサーバーを動かさない、SQL を画面から受け取らない）
- DuckDB を開いたままにして `dbt build` を妨げない

## 検討した選択肢

### 案 A: backend のプロセス内で MetricFlow と dbt Charts を使う（採用）

- 良い点: 起動済みなので速い。描き方は生成 PR と同じ。
- 悪い点: MetricFlow と dbt Charts の Python API に依存する（MetricFlow の `MetricFlowEngine` は内部 API に近い）。

### 案 B: CLI のサブプロセス

- 良い点: 実装は単純。公開のインターフェースだけを使う。
- 悪い点: 1 回 20 秒では切り口を試しながら見られない。

### 案 C: 結果データを返して React で描く（recharts など）

- 良い点: 速い。
- 悪い点: チャートの描き方が dbt Charts（生成 PR のチャート）と別になり、依存も増える。

### 案 D: `dct serve` を 127.0.0.1 で起動して backend が中継

- 良い点: 起動済みなので速い。
- 悪い点: Host を検証しないサーバーをローカルで常に動かすことになる（DNS リバインディングで任意の SQL を実行される）。

## 決定

案 A を採る。

- **SQL の作成**: backend のプロセス内で MetricFlow を使う（`adp.charts.MetricFlowCompiler`）。`target/semantic_manifest.json` だけを読み、SQL を実行しないクライアントを渡す。DB には接続しない（dbt アダプタ経由だと DuckDB を書き込み用に開いたままになり、`dbt build` を妨げるため）。出力は `mf query --explain` と同じにし、manifest が更新されたら読み直す。データ一覧（#38）の切り口（`mf list dimensions`）も同じものから取る。
- **描画**: dbt Charts の公開 API（`dbt_charts.agent_api.ProjectSession.render_board`）をプロセス内で使い、メモリ上のボード（`InMemoryBoard`）を SVG に描く（`adp.charts.BoardRenderer`）。接続は warehouse の `dbt_charts.yml`（dbt プロファイル経由の `warehouse` だけ。描画の前に検査する）で、DuckDB の外部アクセス禁止が効く。描画のたびに接続を閉じる。
- **画面での表示（O5）**: backend が SVG を返し、frontend は `<img src="data:image/svg+xml,...">` で表示する（`<img>` の SVG はスクリプトを実行しない）。`dct serve` は使わない。
- **入力**: 画面から受け取るのは指標名・切り口名・粒度・期間だけ。指標と切り口は Semantic Layer の一覧と照らし合わせ、ボードは生成時と同じ許可リスト（ADR 0002）を通す。SQL は MetricFlow が作ったものだけ。
- **生成 PR のプレビュー**: 生成ジョブの worktree（モデルを build 済み）を同じ仕組みで描く。マージ前に「確定した指標をその場で見る」（#43 の完了条件）。worktree の場所はジョブの記録（ブランチ名）から backend が決める。

## 結果

- 良い影響: 2 回目以降の描き直しは、SQL の作成が数十ミリ秒、描画が約 0.15 秒（初回はそれぞれ 1〜2.5 秒。2026-10-11）。
- 悪い影響・トレードオフ:
  - MetricFlow（0.213）と dbt Charts（0.9.x）の Python API に依存する。
  - プロセス内の dbt Charts は、環境変数 `DBT_PROFILES_DIR` / `ADP_DUCKDB_PATH` でプロファイルや DB の場所が変わる（CLI のように固定できない）。設定されていれば描画を止めて理由を返す。
  - 切り口の値が多いと SVG が十数 MB になるので、3 MB を超えたら描かずに、粒度・期間・切り口を変えるよう返す。
- 後続の作業・ADR: ADR 0001 の決定 3（「backend からは `mf` CLI をサブプロセスで呼ぶ」）を、描画画面とデータ一覧については変えた。生成パイプライン・`adp.charts compile / check`・検証は、これまでどおり CLI を使う。

## 守らせ方

- `backend/tests/test_chart_view.py`: プロセス内の MetricFlow が `mf query --explain` と同じ SQL を作ること。
- `backend/tests/test_datasets.py`・`backend/tests/test_charts.py`: バージョンを上げるときに、Python API の振る舞いを確かめる。

## 追記

- 2026-10-16: ADR のテンプレートの変更に合わせて構成を直した（決定の中身は変えていない）。
