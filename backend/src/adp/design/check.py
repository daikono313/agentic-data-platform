"""インタビュー中に設計の課題を確かめる（#46）。

BEAM の確定条件を満たしていても、ソースの結合の経路が無いなど、設計（スター設計）で止まる
ことがある。確定してから生成ジョブで失敗しないよう、インタビューの「確定までに残っている課題」に
含めて、エージェントに直させる。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from adp.beam import BeamModel
from adp.catalog.catalog import Catalog
from adp.design.builder import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.ontology.models import Ontology, load


def design_issues(
    catalog: Catalog, ontology_path: Path | None = None
) -> Callable[[BeamModel], list[str]]:
    """ontology_path があれば、オントロジーの概念との食い違いも課題にする（#69）。"""
    schema = CatalogSchema(catalog)

    def check(beam: BeamModel) -> list[str]:
        builder = DesignBuilder(schema, ontology=load_ontology(ontology_path))
        return [i.message for i in builder.build(beam).blockers]

    return check


def load_ontology(path: Path | None) -> Ontology | None:
    """オントロジーを読む。無い・壊れている場合は None（設計の確認は続ける）。"""
    if path is None or not path.exists():
        return None
    try:
        return load(path)
    except ValueError:
        return None
