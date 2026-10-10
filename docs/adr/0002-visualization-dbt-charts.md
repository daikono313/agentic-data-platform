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
2. **指標の定義は Semantic Layer に一元化したまま**にする（design-doc R5）。dbt Charts は Semantic Layer に未対応なので（今後の予定とする紹介記事はあるが、公式の約束ではない: [oflight の記事](https://www.oflight.co.jp/en/columns/dbt-charts-yaml-dashboards-ai-agents-2026)）、チャートから MetricFlow の指標を使う仕組みを作る（方式は O4、#41 で決める）。チャートの中で集計を書き直さない。生成するチャート定義に手書きの SQL を入れず、CI で確認する。
   - GitHub のリポジトリは非公開の upstream のミラーで PR を受け付けない（PyPI の README）。そのため O4 の (c)「本体に機能を足す」は、ローカルの拡張・フォークに限られる。
3. 範囲は「Semantic Layer の指標を、決まった切り口・期間・チャートの種類で見る画面」まで。ダッシュボードの自由なレイアウト編集、アドホックな SQL、権限管理は作らない。

## 検討した代替案

- **Evidence / Rill**: コードで定義する BI。dbt プロジェクトとの一体感と、YAML による生成のしやすさで dbt Charts を優先する。dbt Charts の PoC で問題が出たら再検討する。
- **Lightdash**: dbt Semantic Layer に対応する（[ドキュメント](https://docs.lightdash.com/guides/dbt-semantic-layer)。連携の提供範囲はプランによる）。dbt platform の Semantic Layer API を前提にしており、ローカルの MetricFlow + DuckDB の構成（ADR 0001）とは合わない。
- **自前の React チャート（recharts など）**: 実装は簡単だが、チャートの定義が dbt プロジェクトの外に出て、PR でレビューしにくい。O5 の表示方法として、結果データだけを React で描く選択肢は残す。

## 影響

- dbt Charts は pre-1.0（README は Beta、PyPI の分類は Alpha）。バージョンは 0.9.x に固定する。チャートの種類は基本 16 種（ファネルなどは基本の種類の組み合わせで提供、ゲージは無い）。BEAM の指標要求から作る既定のチャート（折れ線・棒）には足りる。
- 依存の整合: dbt-charts 0.9.1 を加えても、このリポジトリの固定バージョン（dbt-core 1.12.5、dbt-duckdb 1.11.0、duckdb 1.5.x、metricflow 0.213.0）のまま解決できる（`uv pip compile` で確認）。pyarrow、vl-convert-python、pillow などが増える。
- 新しい信頼の境界（`dct serve` の認証の無いサーバー、チャート定義の SQL、生成エージェントのガード、DuckDB の外部アクセス禁止）は design-doc の Security に記録した。
