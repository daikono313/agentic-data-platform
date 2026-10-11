"""インタビュー中に設計の課題を確かめる（#46）。

BEAM の確定条件を満たしていても、ソースの結合の経路が無いなど、設計（スター設計）で止まる
ことがある。確定してから生成ジョブで失敗しないよう、インタビューの「確定までに残っている課題」に
含めて、エージェントに直させる。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from adp.beam import BeamModel
from adp.beam import load as load_beam
from adp.catalog.catalog import Catalog
from adp.design.builder import DesignBuilder, grain_sources
from adp.design.catalog_schema import CatalogSchema
from adp.design.models import ExistingFact
from adp.ontology.models import Ontology, load


def design_issues(
    catalog: Catalog, ontology_path: Path | None = None
) -> Callable[[BeamModel], list[str]]:
    """ontology_path があれば、オントロジーの概念との食い違いも課題にする（#69）。

    既存の fct の種類は、オントロジーと同じリポジトリの beam/ から読む（#113）。
    """
    schema = CatalogSchema(catalog)
    beam_dir = ontology_path.parent.parent / "beam" if ontology_path is not None else None

    def check(beam: BeamModel) -> list[str]:
        builder = DesignBuilder(
            schema,
            ontology=load_ontology(ontology_path),
            existing_facts=existing_facts(beam_dir) if beam_dir is not None else None,
        )
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


log = logging.getLogger(__name__)


def existing_facts(beam_dir: Path) -> dict[str, ExistingFact]:
    """確定した BEAM 成果物（beam/*.yml）にある fct と、その種類・粒度。

    BEAM 成果物どうしで種類か粒度が食い違う fct は、種類を None にして理由を conflict に書く
    （どちらかを読む順で選ばない）。読めない成果物はログに出して飛ばす。
    """
    out: dict[str, ExistingFact] = {}
    if not beam_dir.is_dir():
        return out
    for path in sorted(beam_dir.glob("*.yml")):
        try:
            beam = load_beam(path)
        except Exception as e:  # noqa: BLE001  壊れた成果物で設計の確認を止めない
            log.warning("BEAM 成果物 %s を読めないため、既存の fct の確認に使いません: %s", path, e)
            continue
        for event in beam.events:
            grain = grain_sources(event)
            known = out.get(event.fact_table)
            if known is None:
                out[event.fact_table] = ExistingFact(
                    fact_type=event.fact_type, grain_sources=grain, beams=[beam.name]
                )
                continue
            known.beams.append(beam.name)
            if known.conflict is None and (
                known.fact_type != event.fact_type or known.grain_sources != grain
            ):
                first = known.beams[0]
                known.conflict = (
                    f"BEAM 成果物 {first} と {beam.name} で種類・粒度が食い違います"
                    f"（{known.fact_type.value if known.fact_type else '-'} / "
                    f"{event.fact_type.value}）"
                )
                known.fact_type = None
    return out
