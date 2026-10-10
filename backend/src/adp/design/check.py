"""インタビュー中に設計の課題を確かめる（#46）。

BEAM の確定条件を満たしていても、ソースの結合の経路が無いなど、設計（スター設計）で止まる
ことがある。確定してから生成ジョブで失敗しないよう、インタビューの「確定までに残っている課題」に
含めて、エージェントに直させる。
"""

from __future__ import annotations

from collections.abc import Callable

from adp.beam import BeamModel
from adp.catalog.catalog import Catalog
from adp.design.builder import DesignBuilder
from adp.design.catalog_schema import CatalogSchema


def design_issues(catalog: Catalog) -> Callable[[BeamModel], list[str]]:
    schema = CatalogSchema(catalog)

    def check(beam: BeamModel) -> list[str]:
        return [i.message for i in DesignBuilder(schema).build(beam).blockers]

    return check
