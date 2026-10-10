# 0002: 可視化に dbt Charts を使い、Semantic Layer の指標を描画する

- 日付: 2026-10-10
- ステータス: Accepted（#40 の PoC で確認。2026-10-10）
- 関連: #37, #40, #41, #43、design-doc の Goals 6、Non-goals、O4、O5

## 背景

v2 では、作った指標をその場でチャートで見られるようにする（design-doc Goal 6、Scenario S3）。MVP では「汎用 BI やダッシュボード機能」を範囲外にしていたため、範囲を見直す必要がある。

## 決定（案）

1. 可視化には **dbt Charts**（OSS、Apache-2.0、CLI `dct`）を使う。
   - チャートを YAML で定義し、dbt のアダプター経由で SQL を実行して描画する。DuckDB に対応（`dbt-duckdb` が標準の依存）。
   - dbt プロジェクトの中にチャートの定義を置けるので、生成 PR にチャートを含めやすい（#42）。
2. **指標の定義は Semantic Layer に一元化したまま**にする（design-doc R5）。dbt Charts は Semantic Layer に未対応なので（今後の予定とする紹介記事はあるが、公式の約束ではない: [oflight の記事](https://www.oflight.co.jp/en/columns/dbt-charts-yaml-dashboards-ai-agents-2026)）、チャートから MetricFlow の指標を使う仕組みを作る（方式は O4、#41 で決める）。チャートの中で集計を書き直さない。生成するチャート定義に手書きの SQL を入れず、CI で確認する。
   - GitHub のリポジトリは非公開の upstream のミラーで PR を受け付けない（PyPI の README）。そのため O4 の (c)「本体に機能を足す」は、ローカルの拡張・フォークに限られる。
3. 範囲は「Semantic Layer の指標を、決まった切り口・期間・チャートの種類で見る画面」まで。ダッシュボードの自由なレイアウト編集、アドホックな SQL、権限管理は作らない。

## PoC の結果（#40、dbt-charts 0.9.1）

- **接続**: `warehouse/dbt_charts.yml` に `type: dbt_profile`（profile: adp）のソースを置くと、dbt の `profiles.yml` 経由で DuckDB に接続できる。DuckDB のパスは profiles.yml（`ADP_DUCKDB_PATH`）に従うので、生成パイプラインのコピーした DB でもそのまま使える（O9 を解決）。
- **描画**: 生成済みの `fct_order_lines` に対するボードを、`dct render` で text / SVG に出力できた（`backend/tests/test_dbt_charts.py`）。
- **安全（DuckDB の SQL）**: `type: dbt_profile` のソースでは、profiles.yml の `enable_external_access: false` が dbt Charts からの接続にも効く（`read_text` が「disabled by configuration」で拒否されることをテストで確認）。dbt Charts の SQL ガードは SELECT 系しか通さず、`SET` / `ATTACH` / `COPY` / `INSTALL` を拒否する。ボードの Jinja はサンドボックスで、`env_var` は使えない。
- **安全（DuckDB の外の経路）**: 次の 2 つは DuckDB の設定では防げないので、別に塞ぐ。
  - `dbt_charts.yml` に独自の `type: duckdb` ソース（`attach:` など）を書くと、外部アクセスを有効に戻せる → 生成エージェントのガードで `dbt_charts.yml` を書き換え不可にした。ソースは `warehouse`（dbt_profile）の 1 つだけにする。
  - ボードの `type: http` のクエリや、地図の `geo_source` / `geo` の URL は、描画時に HTTP で取得される → 生成するボードに URL と `type: http` を入れない検査を、ボードの生成（#41 / #42）に入れる。
- **`dct serve`**: 既定で localhost だけで待ち受けるが、Host ヘッダーを検証しない（DNS リバインディングで読める）。ブラウザから直接使わない（O5）。
- **依存**: このリポジトリの固定バージョンのまま追加できた（backend の依存に `dbt-charts>=0.9.1,<0.10`）。

## Semantic Layer の指標を使う方式（#41、O4 の決定）

- チャートの元ファイル `warehouse/chart_specs/<board>.yml` には `metric_queries`（指標・切り口・並び順・件数・絞り込み）だけを書き、SQL は書かない。
- 絞り込みは `filters: [{dimension, op, value}]` の構造化した形で書き、MetricFlow の where（`{{ Dimension('...') }} = '値'`）は adp が組み立てる。任意の SQL 式は書けない。MetricFlow と dbt Charts はどちらも SQL を Jinja で展開するので、値に `{ } %` やバックスラッシュは使えず、生成した SQL にテンプレート記法が残っていれば失敗にする。
- `python -m adp.charts compile` が各クエリを MetricFlow で SQL にし（`mf query --explain`）、カタログ名（DB ファイル名）を外して dbt Charts のボード `warehouse/charts/<board>.yml` に埋め込む。
- `python -m adp.charts check`（`make dbt-build` で実行）は、ボードがすべてこの仕組みで作られ、最新であることを確かめる。手書きの SQL のボードは置けない。
- 元ファイルは**許可リスト**で検査する。使えるキーは `title / metric_queries / charts / rows / cols` だけで、チャートの `query` は `metric_queries` の名前に限る。チャートの種類・属性も許可リストに限り、`extends`、レイアウト中のボードの入れ子や別ファイルの読み込み（`.yml` で終わる名前）、HTML（`text`、`html_policy`）、地図（`geo_source` など）は使えない。URL や `type: http` も拒否する。dbt Charts は `charts/` の下の `.yml / .yaml / .md / .markdown` をすべて配信し、シンボリックリンクもたどるので、元ファイルは `charts/` の外（`warehouse/chart_specs/`）に置き、`charts/` と `chart_specs/` の直下には `<名前>.yml` 以外（サブディレクトリ、シンボリックリンク、他の形式）を置けない。`dbt_charts.yml` のソースが dbt プロファイル経由の `warehouse` だけであることも確かめる（DuckDB の外の経路を塞ぐ）。
- チャートの値は `mf query` と一致することをテストで確認した（`backend/tests/test_charts.py`）。
- 案 (b)（結果をテーブルに書き出す）は、データが古くなり、切り口を変えるたびに書き出しが必要になるので採らない。案 (c) は upstream が PR を受け付けないので採らない。

## 検討した代替案

- **Evidence / Rill**: コードで定義する BI。dbt プロジェクトとの一体感と、YAML による生成のしやすさで dbt Charts を優先する。dbt Charts の PoC で問題が出たら再検討する。
- **Lightdash**: dbt Semantic Layer に対応する（[ドキュメント](https://docs.lightdash.com/guides/dbt-semantic-layer)。連携の提供範囲はプランによる）。dbt platform の Semantic Layer API を前提にしており、ローカルの MetricFlow + DuckDB の構成（ADR 0001）とは合わない。
- **自前の React チャート（recharts など）**: 実装は簡単だが、チャートの定義が dbt プロジェクトの外に出て、PR でレビューしにくい。O5 の表示方法として、結果データだけを React で描く選択肢は残す。

## 影響

- dbt Charts は pre-1.0（README は Beta、PyPI の分類は Alpha）。バージョンは 0.9.x に固定する。チャートの種類は基本 16 種（ファネルなどは基本の種類の組み合わせで提供、ゲージは無い）。BEAM の指標要求から作る既定のチャート（折れ線・棒）には足りる。
- 依存の整合: dbt-charts 0.9.1 を加えても、このリポジトリの固定バージョン（dbt-core 1.12.5、dbt-duckdb 1.11.0、duckdb 1.5.x、metricflow 0.213.0）のまま解決できる（`uv pip compile` で確認）。pyarrow、vl-convert-python、pillow などが増える。
- 新しい信頼の境界（`dct serve` の認証の無いサーバー、チャート定義の SQL、生成エージェントのガード、DuckDB の外部アクセス禁止）は design-doc の Security に記録した。
