# beam/

確定した BEAM✲ 成果物（`<event_name>.yml`）を置く。要求の正本であり、対応する dbt モデルと Semantic Layer の生成元になる。

- スキーマは `backend/src/adp/beam/` の Pydantic モデルで定義する。
- **インタビューエージェント（AI）が書き、ユーザー（人間）が画面で確かめて確定する。** アプリが PR を作るときに、生成コードと一緒に commit する。
- 直すときは手で編集せず、画面のチャットで直してから確定し直す（手で編集すると、再生成で差分が出る）。
- 手法については [docs/knowledge/beam.md](../docs/knowledge/beam.md) を参照。
