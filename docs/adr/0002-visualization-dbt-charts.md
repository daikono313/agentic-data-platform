# 0002: 可視化に dbt Charts を使い、Semantic Layer の指標を描画する

- 日付: 2026-10-10
- ステータス: Accepted（2026-10-10、#40 の PoC で確認）
- 関連: #37, #40, #41, #42, #43、design-doc の Goals 6、Non-goals、O4（→ R18）、O6・O9（→ R17）、O5（→ ADR 0003・R20）、ADR 0001
- 見直し: dbt Charts が 1.0 になったとき、または Semantic Layer に対応したとき（0.9.x の固定と、SQL を埋め込む方式を見直す）

## 背景

v2 では、作った指標をその場でチャートで見られるようにする（design-doc Goal 6、Scenario S3）。MVP では「汎用 BI やダッシュボード機能」を範囲外にしていたため、範囲を見直す必要がある。

**dbt Charts の PoC の結果（#40、dbt-charts 0.9.1。2026-10-10）**

- **接続**: `warehouse/dbt_charts.yml` に `type: dbt_profile`（profile: adp）のソースを置くと、dbt の `profiles.yml` 経由で DuckDB に接続できる。DuckDB のパスは profiles.yml（`ADP_DUCKDB_PATH`）に従うので、生成パイプラインのコピーした DB でもそのまま使える（O9 を解決）。
- **描画**: 生成済みの `fct_order_lines` に対するボードを、`dct render` で text / SVG に出力できた（`backend/tests/test_dbt_charts.py`）。
- **安全（DuckDB の SQL）**: `type: dbt_profile` のソースでは、profiles.yml の `enable_external_access: false` が dbt Charts からの接続にも効く（`read_text` が「disabled by configuration」で拒否されることをテストで確認）。dbt Charts の SQL ガードは SELECT 系しか通さず、`SET` / `ATTACH` / `COPY` / `INSTALL` を拒否する。ボードの Jinja はサンドボックスで、`env_var` は使えない。
- **安全（DuckDB の外の経路）**: 次の 2 つは DuckDB の設定では防げない。
  - `dbt_charts.yml` に独自の `type: duckdb` ソース（`attach:` など）を書くと、外部アクセスを有効に戻せる。
  - ボードの `type: http` のクエリや、地図の `geo_source` / `geo` の URL は、描画時に HTTP で取得される。
- **`dct serve`**: 既定で localhost だけで待ち受けるが、Host ヘッダーを検証しない（DNS リバインディングで読める）。
- **Semantic Layer**: dbt Charts は Semantic Layer に未対応（今後の予定とする紹介記事はあるが、公式の約束ではない: [oflight の記事](https://www.oflight.co.jp/en/columns/dbt-charts-yaml-dashboards-ai-agents-2026)）。GitHub のリポジトリは非公開の upstream のミラーで PR を受け付けない（PyPI の README）。
- **依存**: このリポジトリの固定バージョン（dbt-core 1.12.5、dbt-duckdb 1.11.0、duckdb 1.5.x、metricflow 0.213.0）のまま、`dbt-charts>=0.9.1,<0.10` を追加できた（`uv pip compile` で確認）。pyarrow、vl-convert-python、pillow などが増える。

## 判断の基準

- 指標の定義を Semantic Layer に一元化したままにする（design-doc R5）。チャートの中で集計を書き直さない
- チャートの定義を dbt プロジェクトの中に置き、生成 PR に含めてレビューできる（#42）
- DuckDB の外部アクセス禁止を含め、新しい信頼の境界を塞げる
- 範囲を「Semantic Layer の指標を、決まった切り口・期間・チャートの種類で見る画面」までに抑える

## 検討した選択肢

### 可視化のツール

#### 案 A: dbt Charts（OSS、Apache-2.0、CLI `dct`）（採用）

- 良い点: チャートを YAML で定義し、dbt のアダプター経由で SQL を実行して描画する。DuckDB に対応（`dbt-duckdb` が標準の依存）。dbt プロジェクトの中に置けるので、生成 PR にチャートを含めやすい。
- 悪い点: pre-1.0（README は Beta、PyPI の分類は Alpha）。Semantic Layer に未対応。チャートの種類は基本 16 種（ファネルなどは組み合わせ、ゲージは無い）。

#### 案 B: Evidence / Rill

- 良い点: コードで定義する BI。
- 悪い点: dbt プロジェクトとの一体感と、YAML による生成のしやすさで dbt Charts に劣る。dbt Charts の PoC で問題が出たら再検討する。

#### 案 C: Lightdash

- 良い点: dbt Semantic Layer に対応する（[ドキュメント](https://docs.lightdash.com/guides/dbt-semantic-layer)。連携の提供範囲はプランによる）。
- 悪い点: dbt platform の Semantic Layer API を前提にしており、ローカルの MetricFlow + DuckDB の構成（ADR 0001）とは合わない。

#### 案 D: 自前の React チャート（recharts など）

- 良い点: 実装は簡単。
- 悪い点: チャートの定義が dbt プロジェクトの外に出て、PR でレビューしにくい。O5 の表示方法として、結果データだけを React で描く選択肢は残す。

### Semantic Layer の指標を使う方式（O4、#41）

#### 案 (a): MetricFlow が作る SQL をボードに埋め込む（採用）

- 良い点: 指標の定義は Semantic Layer のまま。チャートの値が `mf query` と一致する。
- 悪い点: 指標を変えたらボードを書き出し直す必要がある（検査で検出する）。

#### 案 (b): 結果をテーブルに書き出す

- 悪い点: データが古くなり、切り口を変えるたびに書き出しが必要になる。

#### 案 (c): dbt Charts の本体に機能を足す

- 悪い点: upstream が PR を受け付けないので、ローカルの拡張・フォークに限られる。

## 決定

可視化は案 A（dbt Charts）、指標の使い方は案 (a) を採る。

1. **ツール**: dbt Charts を 0.9.x に固定して使う。範囲は「Semantic Layer の指標を、決まった切り口・期間・チャートの種類で見る画面」まで。ダッシュボードの自由なレイアウト編集、アドホックな SQL、権限管理は作らない。
2. **元ファイル**: `warehouse/chart_specs/<board>.yml` には `metric_queries`（指標・切り口・並び順・件数・絞り込み）だけを書き、SQL は書かない。絞り込みは `filters: [{dimension, op, value}]` の構造化した形で書き、MetricFlow の where（`{{ Dimension('...') }} = '値'`）は adp が組み立てる。任意の SQL 式は書けない。MetricFlow と dbt Charts はどちらも SQL を Jinja で展開するので、値に `{ } %` やバックスラッシュは使えず、生成した SQL にテンプレート記法が残っていれば失敗にする。
3. **書き出し**: `python -m adp.charts compile` が各クエリを MetricFlow で SQL にし（`mf query --explain`）、カタログ名（DB ファイル名）を外して dbt Charts のボード `warehouse/charts/<board>.yml` に埋め込む。
4. **DuckDB の外の経路を塞ぐ**:
   - `dbt_charts.yml` は生成エージェントのガードで書き換え不可にし、ソースは `warehouse`（dbt_profile）の 1 つだけにする。
   - 元ファイルは許可リストで検査する（ボードの生成 #41 / #42 に入れる）。使えるキーは `title / metric_queries / charts / rows / cols` だけで、チャートの `query` は `metric_queries` の名前に限る。チャートの種類・属性も許可リストに限り、`extends`、レイアウト中のボードの入れ子や別ファイルの読み込み（`.yml` で終わる名前）、HTML（`text`、`html_policy`）、地図（`geo_source` など）、URL、`type: http` は使えない。
   - dbt Charts は `charts/` の下の `.yml / .yaml / .md / .markdown` をすべて配信し、シンボリックリンクもたどるので、元ファイルは `charts/` の外（`warehouse/chart_specs/`）に置く。`charts/` と `chart_specs/` の直下には `<名前>.yml` 以外（サブディレクトリ、シンボリックリンク、他の形式）を置けない。`charts/` が無いときに配信される `faces/` も置けない。
   - `dct serve` はブラウザから直接使わない（表示方法は O5、ADR 0003）。

## 結果

- 良い影響: 生成 PR にチャートが入り、指標の定義は Semantic Layer の 1 か所のまま。
- 悪い影響・トレードオフ: pre-1.0 の依存を 0.9.x に固定して持つ。チャートの種類は BEAM の指標要求から作る既定のチャート（折れ線・棒）には足りるが、ゲージなどは無い。依存が増える（pyarrow など）。
- 後続の作業・ADR: 新しい信頼の境界（`dct serve` の認証の無いサーバー、チャート定義の SQL、生成エージェントのガード、DuckDB の外部アクセス禁止）を design-doc の Security に記録した。画面での表示方法（O5）は ADR 0003 で決めた。

## 守らせ方

- `python -m adp.charts check`（`make dbt-build` で実行）: ボードがすべてこの仕組みで作られ、最新であることを確かめる。手書きの SQL のボードは置けない。`dbt_charts.yml` が決まった内容と完全に一致することも確かめる。
- 生成エージェントのガード（`backend/src/adp/generation/guard.py`）で `dbt_charts.yml` を書き換え不可にしている。
- テスト: チャートの値が `mf query` と一致すること（`backend/tests/test_charts.py`）、外部アクセスの拒否（`backend/tests/test_dbt_charts.py`）。

## 追記

- 2026-10-11: O5 の表示方法は、結果データを React で描く案を採らず、backend のプロセス内で dbt Charts を使って SVG を描くことにした（[ADR 0003](0003-chart-view-in-process.md)）。
- 2026-10-16: ADR のテンプレートの変更に合わせて構成を直した（決定の中身は変えていない。オーナーの承認つき、#171）。もとは「決定（案）」「PoC の結果」「Semantic Layer の指標を使う方式」の節に分かれていた。PoC の結果は背景に、方式の案 (b)・(c) は選択肢に、検査は守らせ方に移した。「判断の基準」「見直し」と、選択肢の良い点・悪い点の一部は、このときに補ったもの（決定した当時の記述ではない）。
