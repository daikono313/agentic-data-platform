# ADR（Architecture Decision Records）

[design-doc.md](../design-doc.md) の確定後に、その決定（Resolved issues）を変える、または新しく重要な決定をするときに書く。

- ファイル名: `NNNN-<kebab-case-title>.md`（例: `0001-metricflow-on-duckdb.md`）
- 書いたら、design-doc の Resolved issues / Open issues も更新する。

## テンプレート

```markdown
# NNNN: <タイトル>

- 日付: YYYY-MM-DD
- ステータス: Proposed | Accepted | Superseded by NNNN
- 関連: #<issue>, design-doc の R<n> / O<n>

## 背景
## 決定
## 検討した代替案
## 影響
```
