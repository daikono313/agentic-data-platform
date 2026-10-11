# backend（FastAPI / Python）

- Python 3.12、パッケージ管理は uv。ソースは `src/adp/`、テストは `tests/`（モジュール構成と同じ並び）。
- モジュールの責務は [design-doc のコンポーネント表](../docs/design-doc.md#コンポーネント) に従う。依存の向きは `api → (interview | design | generation | vcs) → (beam | catalog | llm | ontology | cleansing)`。`beam` は他のどのモジュールにも依存しない。`ontology` と `cleansing` は `catalog` だけに依存する。
- `adp.beam` の Pydantic モデルが BEAM 成果物の正本。JSON Schema は `uv run python -m adp.beam.schema` で出力し、frontend の型はそこから生成する（手で書かない）。
- LLM の呼び出しは `adp.llm` の経由に限る。テストでは `adp.llm` のフェイクを使い、実際の LLM を呼ばない。
- 生成エージェントのファイル操作は `.adp/worktrees/<branch>` の中に限る。
- lint は ruff と mypy（strict）。
