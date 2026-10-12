# backend（FastAPI / Python）

- Python 3.12、パッケージ管理は uv。ソースは `src/adp/`、テストは `tests/`（モジュール構成と同じ並び）。
- モジュールの責務は [design-doc のコンポーネント表](../docs/design-doc.md#コンポーネント) に従う。依存の向きは `api → (interview | design | generation | vcs | onboarding) → (beam | catalog | llm | ontology | cleansing)`（`generation` は design を、`vcs` は design・generation を、`onboarding` は generation・vcs・pipeline・datasets も使う）。`beam` は他のどのモジュールにも依存しない。`ontology` と `cleansing` は `catalog`（と下の共有モジュール）だけに依存する。どこからでも使える小さな共有モジュール `adp.warehouse`（接続先）・`adp.markdown`（文章の無害化）・`adp.metricflow`（where のリテラル）・`adp.yaml_docs`（ディレクトリの YAML の読み込み。壊れたファイルを飛ばし、変わらないファイルは読み直さない）は、他の adp のモジュールに依存しない（`adp.warehouse` だけは `.env` を読むために `adp.llm.settings` を使う）。
- `adp.beam` の Pydantic モデルが BEAM 成果物の正本。JSON Schema は `uv run python -m adp.beam.schema` で出力し、frontend の型はそこから生成する（手で書かない）。
- LLM の呼び出しは `adp.llm` の経由に限る。テストでは `adp.llm` のフェイクを使い、実際の LLM を呼ばない。
- 生成エージェントのファイル操作は `.adp/worktrees/<branch>` の中に限る。
- lint は ruff と mypy（strict）。
