"""源泉の取り込み（#69）: 追加案・検証・バスの差分・エージェント・設計の検査。"""

from pathlib import Path
from typing import Any

import duckdb
import pytest

from adp.catalog.catalog import Catalog
from adp.datasets import BusGroup, BusMatrix, BusRow
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.llm import LLMSettings, ScriptedRunner
from adp.onboarding.agent import propose_with_agent
from adp.onboarding.plan import bus_with_candidates
from adp.onboarding.proposal import OntologyPatch, apply, propose_from_profile, validate_patch
from adp.ontology import load
from adp.ontology.models import link_models

REPO = Path(__file__).resolve().parents[3]
ONTOLOGY = load(REPO / "ontology" / "ontology.yml")


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    """オントロジーの源泉（の一部）と、新しい源泉 raw.inventory。"""
    db = tmp_path / "x.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute(
        "create table raw.stores as select range + 1 as store_id, 'S' || range as store_name,"
        " 'store' as channel, null as prefecture, null as region,"
        " date '2020-01-01' as opened_on from range(3)"
    )
    con.execute(
        "create table raw.products as select range + 1 as product_id, 'P' as product_name,"
        " 'c' as category, 's' as subcategory, 'b' as brand, 100 as list_price,"
        " 50 as unit_cost, date '2020-01-01' as launched_on from range(3)"
    )
    con.execute(
        "create table raw.inventory as select range + 1 as inventory_id,"
        " range % 3 + 1 as store_id, range % 3 + 1 as product_id,"
        " timestamp '2024-01-01' + interval (range) hour as moved_at, 'receipt' as movement_type,"
        " 5 as quantity, timestamp '2024-01-02' as updated_at from range(30)"
    )
    con.close()
    return Catalog(db)


def test_profile_proposal_is_valid(catalog: Catalog) -> None:
    patch = propose_from_profile(catalog.profile_table("raw.inventory"), ONTOLOGY)
    concept = patch.concepts[0]
    assert concept.name == "inventory" and concept.kind == "event" and concept.key == "inventory_id"
    assert {a.column for a in concept.attributes} == {"moved_at", "movement_type", "quantity"}
    assert {(r.from_, r.via) for r in patch.relations} == {
        ("store", "raw.inventory.store_id"),
        ("product", "raw.inventory.product_id"),
    }
    assert validate_patch(ONTOLOGY, patch, catalog, "raw.inventory") == []


def test_invalid_patches_are_reported(catalog: Catalog) -> None:
    base = propose_from_profile(catalog.profile_table("raw.inventory"), ONTOLOGY)
    data = base.model_dump(mode="json", by_alias=True)
    data["relations"][0]["via"] = "raw.inventory.quantity"  # キーではない列
    errors = validate_patch(ONTOLOGY, OntologyPatch.model_validate(data), catalog, "raw.inventory")
    assert errors and "raw.inventory.quantity" in errors[0]
    data = base.model_dump(mode="json", by_alias=True)
    data["concepts"][0]["name"] = "customer"  # 既存の概念と重複
    assert validate_patch(ONTOLOGY, OntologyPatch.model_validate(data), catalog, "raw.inventory")
    other = base.model_copy(
        update={"concepts": [base.concepts[0].model_copy(update={"source": "raw.stores"})]}
    )
    assert validate_patch(ONTOLOGY, other, catalog, "raw.inventory") == [
        "源泉 raw.inventory を元にした概念がありません"
    ]


def test_bus_candidates(catalog: Catalog) -> None:
    patch = propose_from_profile(catalog.profile_table("raw.inventory"), ONTOLOGY)
    bus = BusMatrix(
        groups=[
            BusGroup(name="dim_store", label="店舗", kind="dim"),
            BusGroup(name="dim_date", label="日付", kind="dim"),
        ],
        rows=[BusRow(name="fct_order_lines", label="注文", groups=["dim_store"])],
    )
    out = bus_with_candidates(bus, ONTOLOGY, patch)
    row = next(r for r in out.rows if r.name == "fct_inventory")
    assert row.candidate and set(row.groups) == {"dim_store", "dim_date"}  # dim_product は表に無い


def _script(patches: list[dict[str, Any]]) -> list[list[Any]]:
    return [[("submit_proposal", {"patch": p}) for p in patches]]


async def test_agent_proposal_is_validated_and_retried(catalog: Catalog) -> None:
    good = propose_from_profile(catalog.profile_table("raw.inventory"), ONTOLOGY)
    good_json = good.model_dump(mode="json", by_alias=True)
    bad = {
        **good_json,
        "relations": [{**good_json["relations"][0], "via": "raw.inventory.quantity"}],
    }
    runner = ScriptedRunner(_script([bad, good_json]))
    result = await propose_with_agent(runner, LLMSettings(), ONTOLOGY, catalog, "raw.inventory")
    assert result.patch == good and result.attempts == 2
    assert "直してください" in str(runner.tool_results[0]) and runner.tool_results[0]["is_error"]


async def test_agent_without_valid_proposal_reports_error(catalog: Catalog) -> None:
    runner = ScriptedRunner(_script([{"concepts": []}]))
    result = await propose_with_agent(runner, LLMSettings(), ONTOLOGY, catalog, "raw.inventory")
    assert result.patch is None and result.error


def test_link_models() -> None:
    patch = OntologyPatch.model_validate(
        {
            "concepts": [
                {
                    "name": "inventory",
                    "label": "在庫",
                    "domain": "channel",
                    "kind": "event",
                    "source": "raw.inventory",
                    "key": "inventory_id",
                },
            ]
        }
    )
    onto = apply(ONTOLOGY, patch)
    linked, names = link_models(
        onto, [("raw.inventory", "fct_inventory_movements"), ("raw.stores", "dim_other")]
    )
    assert names == ["inventory"]  # 既にモデルのある概念（store）は変えない
    assert linked.concept("inventory").model == "fct_inventory_movements"
    assert linked.concept("store").model == "dim_store"
    assert link_models(onto, [("raw.inventory", "dim_x")])[1] == []  # 種類と接頭辞が合わない


def test_design_blocks_dimension_that_conflicts_with_ontology(catalog: Catalog) -> None:
    """同じ源泉の概念に dim があるのに別の名前の dim を作ろうとすると、課題にする。"""
    from adp.beam import load as load_beam

    beam = load_beam(REPO / "backend/tests/fixtures/beam/sales_by_store.yml")
    data = beam.model_dump(mode="json")
    for d in data["events"][0]["dimensions"]:
        if d["name"] == "store":
            d["name"] = "shop"
    renamed = type(beam).model_validate(data)
    db_catalog = Catalog(REPO / "warehouse/data/dev.duckdb")
    if not (REPO / "warehouse/data/dev.duckdb").exists():
        pytest.skip("make setup が必要")
    issues = DesignBuilder(CatalogSchema(db_catalog), ontology=ONTOLOGY).build(renamed).blockers
    assert any("dim_shop" in i.message and "dim_store を再利用" in i.message for i in issues)
    assert not DesignBuilder(CatalogSchema(db_catalog), ontology=ONTOLOGY).build(beam).blockers
