"""既にある指標との重複（#75）。"""

import json
from pathlib import Path

from adp.beam import load
from adp.design.check import duplicate_metrics

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "beam" / "sales_by_store.yml"


def _repo(tmp_path: Path, metrics: list[dict[str, str]]) -> Path:
    (tmp_path / "warehouse" / "target").mkdir(parents=True)
    (tmp_path / "warehouse" / "target" / "semantic_manifest.json").write_text(
        json.dumps({"metrics": metrics}), encoding="utf-8"
    )
    return tmp_path


def test_same_name_or_label_is_an_issue(tmp_path: Path) -> None:
    beam = load(FIXTURE)  # 指標 revenue（売上金額）
    repo = _repo(tmp_path, [{"name": "revenue", "label": "売上"}])
    [issue] = duplicate_metrics(beam, repo)
    assert "指標の名前 revenue は既にあります" in issue and "チャートで見られます" in issue

    # 表示名は、括弧・空白・全角半角の違いを無視して比べる
    repo = _repo(tmp_path / "b", [{"name": "sales", "label": "売上 金額"}])
    [issue] = duplicate_metrics(beam, repo)
    assert "表示名「売上金額」の指標は既にあります（sales）" in issue

    assert duplicate_metrics(beam, _repo(tmp_path / "c", [{"name": "x", "label": "粗利"}])) == []


def test_reconfirming_the_same_beam_is_not_a_duplicate(tmp_path: Path) -> None:
    """同じ BEAM 成果物を確定し直すとき（beam/<name>.yml にある指標）は重複にしない。"""
    beam = load(FIXTURE)
    repo = _repo(tmp_path, [{"name": "revenue", "label": "売上金額"}])
    (repo / "beam").mkdir()
    (repo / "beam" / f"{beam.name}.yml").write_text(FIXTURE.read_text("utf-8"), encoding="utf-8")
    assert duplicate_metrics(beam, repo) == []


def test_without_manifest_there_is_nothing_to_compare(tmp_path: Path) -> None:
    assert duplicate_metrics(load(FIXTURE), tmp_path) == []
