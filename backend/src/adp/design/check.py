"""インタビュー中に設計の課題を確かめる（#46）。

BEAM の確定条件を満たしていても、ソースの結合の経路が無いなど、設計（スター設計）で止まる
ことがある。確定してから生成ジョブで失敗しないよう、インタビューの「確定までに残っている課題」に
含めて、エージェントに直させる。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

import yaml

from adp.beam import BeamModel
from adp.catalog.catalog import Catalog
from adp.design.builder import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.ontology.models import Ontology, load


def design_issues(
    catalog: Catalog, ontology_path: Path | None = None, repo_root: Path | None = None
) -> Callable[[BeamModel], list[str]]:
    """ontology_path があれば、オントロジーの概念との食い違いも課題にする（#69）。

    repo_root があれば、既にある指標との重複も課題にする（#75）。
    """
    schema = CatalogSchema(catalog)

    def check(beam: BeamModel) -> list[str]:
        builder = DesignBuilder(schema, ontology=load_ontology(ontology_path))
        issues = [i.message for i in builder.build(beam).blockers]
        if repo_root is not None:
            issues += duplicate_metrics(beam, repo_root)
        return issues

    return check


def _normalize(label: str) -> str:
    """表示名の比較用（全角・半角の括弧・空白の違いを無視する）。"""
    return re.sub(r"[\s（）()「」・]", "", unicodedata.normalize("NFKC", label)).lower()


def duplicate_metrics(beam: BeamModel, repo_root: Path) -> list[str]:
    """既にある指標（Semantic Layer）と、名前か表示名が同じ指標（#75）。

    同じ BEAM 成果物の確定し直し（beam/<name>.yml にある指標）は除く。同じなら作らずに
    チャートで見られることを、違うなら名前を変えることを促す。
    """
    try:
        manifest = json.loads(
            (repo_root / "warehouse" / "target" / "semantic_manifest.json").read_text("utf-8")
        )
    except (OSError, ValueError):
        return []
    own: set[str] = set()
    own_path = repo_root / "beam" / f"{beam.name}.yml"
    if own_path.exists():
        try:
            own = {
                m["name"] for m in yaml.safe_load(own_path.read_text("utf-8")).get("metrics", [])
            }
        except (yaml.YAMLError, AttributeError, KeyError, TypeError):
            own = set()
    existing = {
        m["name"]: m.get("label") or m["name"]
        for m in manifest.get("metrics", [])
        if m.get("name") and m["name"] not in own
    }
    labels = {_normalize(label): name for name, label in existing.items()}
    issues = []
    for m in beam.metrics:
        if m.name in own:
            continue
        if m.name in existing:
            issues.append(
                f"指標の名前 {m.name} は既にあります（「{existing[m.name]}」）。"
                "同じ指標なら作らずにチャートで見られます。違う指標なら名前を変えてください"
            )
        elif _normalize(m.label) in labels:
            other = labels[_normalize(m.label)]
            issues.append(
                f"表示名「{m.label}」の指標は既にあります（{other}）。"
                "同じ指標なら作らずにチャートで見られます。違う指標なら表示名を変えてください"
            )
    return issues


def load_ontology(path: Path | None) -> Ontology | None:
    """オントロジーを読む。無い・壊れている場合は None（設計の確認は続ける）。"""
    if path is None or not path.exists():
        return None
    try:
        return load(path)
    except ValueError:
        return None
