"""ProjectInfo の models/ の YAML の読み込み（壊れたファイルで生成全体を落とさない。#110）。"""

from pathlib import Path

from adp.catalog.catalog import Catalog
from adp.generation.project import ProjectInfo


def _project(tmp_path: Path) -> ProjectInfo:
    models = tmp_path / "models"
    (models / "marts").mkdir(parents=True)
    (models / "marts" / "fct_a.yml").write_text(
        "models:\n  - name: fct_a\n    columns:\n      - name: a_id\n"
        "      - description: name の無い列\n      - name: amount\n"
        "  - description: name の無いモデル\n",
        encoding="utf-8",
    )
    (models / "marts" / "fct_a.sql").write_text("select 1 as a_id\n", encoding="utf-8")
    (models / "marts" / "broken.yml").write_text("models: [\n  - name: :\n", encoding="utf-8")
    (models / "staging").mkdir()
    (models / "staging" / "_sources.yml").write_text(
        "sources:\n  - name: raw\n    tables:\n      - name: orders\n      - identifier: x\n"
        "  - schema: s\n",
        encoding="utf-8",
    )
    return ProjectInfo(tmp_path, Catalog(tmp_path / "data" / "dev.duckdb"))


def test_broken_yaml_and_entries_without_name_are_skipped(tmp_path: Path) -> None:
    info = _project(tmp_path)
    assert info.existing_columns("fct_a") == {"a_id", "amount"}
    assert info.existing_columns("fct_missing") == set()
    assert info.declared_sources() == {"raw.orders": "raw"}
    found = info.model_file("fct_a", ".yml")
    assert found is not None and found[0] == "models/marts/fct_a.yml" and "fct_a" in found[1]
    assert info.model_file("fct_a", ".sql") == ("models/marts/fct_a.sql", "select 1 as a_id\n")
    assert info.model_file("fct_missing", ".yml") is None


def test_changed_yaml_is_seen(tmp_path: Path) -> None:
    info = _project(tmp_path)
    assert info.existing_columns("fct_a") == {"a_id", "amount"}
    (tmp_path / "models" / "marts" / "fct_a.yml").write_text(
        "models:\n  - name: fct_a\n    columns:\n      - name: a_id\n      - name: amount\n"
        "      - name: qty\n",
        encoding="utf-8",
    )
    assert info.existing_columns("fct_a") == {"a_id", "amount", "qty"}
