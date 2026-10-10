# 0002: 可視化に dbt Charts を使い、Semantic Layer の指標を描画する

- 日付: 2026-10-10
- ステータス: Proposed（#40 の PoC の結果で Accepted / 変更）
- 関連: #37, #40, #41, #43、design-doc の Goals 6、Non-goals、O4、O5

## 背景

v2 では、作った指標をその場でチャートで見られるようにする（design-doc Goal 6、Scenario S3）。MVP では「汎用 BI やダッシュボード機能」を範囲外にしていたため、範囲を見直す必要がある。

## 決定（案）

1. 可視化には **dbt Charts**（OSS、Apache-2.0、CLI `dct`）を使う。
   - チャートを YAML で定義し、dbt のアダプター経由で SQL を実行して描画する。DuckDB に対応（`dbt-duckdb` が標準の依存）。
   - dbt プロジェクトの中にチャートの定義を置けるので、生成 PR にチャートを含めやすい（#42）。
2. **指標の定義は Semantic Layer に一元化したまま**にする（design-doc R5）。dbt Charts は Semantic Layer に未対応（公式でも今後の予定）なので、チャートから MetricFlow の指標を使う仕組みを作る（方式は O4、#41 で決める）。チャートの中で集計を書き直さない。
3. 範囲は「Semantic Layer の指標を、決まった切り口・期間・チャートの種類で見る画面」まで。ダッシュボードの自由なレイアウト編集、アドホックな SQL、権限管理は作らない。

## 検討した代替案

- **Evidence / Rill**: コードで定義する BI。dbt プロジェクトとの一体感と、YAML による生成のしやすさで dbt Charts を優先する。dbt Charts の PoC で問題が出たら再検討する。
- **Lightdash**: dbt Semantic Layer に対応するが、連携は有料プランで、ローカルの DuckDB 前提の MVP には重い。
- **自前の React チャート（recharts など）**: 実装は簡単だが、チャートの定義が dbt プロジェクトの外に出て、PR でレビューしにくい。O5 の表示方法として、結果データだけを React で描く選択肢は残す。

## 影響

- dbt Charts は Beta で、チャートの種類は 16 種（ファネル・ゲージなどは無い）。BEAM の指標要求から作る既定のチャート（折れ線・棒）には足りる。
- 依存（dbt-core / dbt-duckdb / duckdb のバージョン範囲）の整合を #40 で確認する。
