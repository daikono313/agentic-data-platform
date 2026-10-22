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
from adp.ontology.models import link_models
from tests.ontology_before_inventory import ontology as before_inventory

REPO = Path(__file__).resolve().parents[3]
ONTOLOGY = before_inventory()  # 在庫を取り込む前（在庫を後から増えた源泉として使う）


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
    errors = validate_patch(ONTOLOGY, OntologyPatch.model_validate(data), catalog, "raw.inventory")
    assert any("重複" in e for e in errors), errors
    other = base.model_copy(
        update={"concepts": [base.concepts[0].model_copy(update={"source": "raw.stores"})]}
    )
    assert "源泉 raw.inventory を元にした概念がありません" in validate_patch(
        ONTOLOGY, other, catalog, "raw.inventory"
    )


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
    # 状態（定期的に測る値）の概念も fct の候補の行になる（ADR 0008）
    state = patch.model_copy(
        update={"concepts": [c.model_copy(update={"kind": "state"}) for c in patch.concepts]}
    )
    out = bus_with_candidates(bus, ONTOLOGY, state)
    row = next(r for r in out.rows if r.name == "fct_inventory")
    assert row.candidate and set(row.groups) == {"dim_store", "dim_date"}
    # 状態の概念の案も、検証を通って適用できる
    assert validate_patch(ONTOLOGY, state, catalog, "raw.inventory") == []
    assert apply(ONTOLOGY, state).concept("inventory").kind == "state"


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
        onto,
        [
            ("raw.inventory", "fct_inventory_movements", "event"),
            ("raw.stores", "dim_other", "entity"),
        ],
    )
    assert names == ["inventory"]  # 既にモデルのある概念（store）は変えない
    assert linked.concept("inventory").model == "fct_inventory_movements"
    assert linked.concept("store").model == "dim_store"
    assert link_models(onto, [("raw.inventory", "dim_x", "entity")])[1] == []  # 種類が合わない


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


def test_patch_must_only_add_the_source(catalog: Catalog) -> None:
    """追加案は取り込む源泉の概念だけ。モデルは書かない。既にある源泉は不可（#87 レビュー）。"""
    base = propose_from_profile(catalog.profile_table("raw.inventory"), ONTOLOGY)
    extra = base.concepts[0].model_copy(
        update={"name": "store_dup", "source": "raw.stores", "key": "store_id"}
    )
    assert any(
        "raw.stores" in e
        for e in validate_patch(
            ONTOLOGY,
            base.model_copy(update={"concepts": [*base.concepts, extra]}),
            catalog,
            "raw.inventory",
        )
    )
    with_model = base.concepts[0].model_copy(update={"model": "fct_x"})
    assert any(
        "model" in e
        for e in validate_patch(
            ONTOLOGY, base.model_copy(update={"concepts": [with_model]}), catalog, "raw.inventory"
        )
    )
    store = base.concepts[0].model_copy(
        update={"name": "shop", "source": "raw.stores", "key": "store_id"}
    )
    errors = validate_patch(
        ONTOLOGY,
        base.model_copy(update={"concepts": [store], "relations": []}),
        catalog,
        "raw.stores",
    )
    assert "raw.stores は既にオントロジーにあります" in errors


def test_pr_body_neutralizes_mentions_and_newlines() -> None:
    from adp.onboarding.pipeline import OnboardingPipeline

    patch = OntologyPatch.model_validate(
        {
            "concepts": [
                {
                    "name": "inventory",
                    "label": "在庫\n\nCloses #82 @octocat ![x](https://evil/x.png)",
                    "domain": "channel",
                    "kind": "event",
                    "source": "raw.inventory",
                    "key": "inventory_id",
                }
            ]
        }
    )
    body = OnboardingPipeline._body("raw.inventory", patch, ["ontology/ontology.yml"])
    assert "Closes #82" not in body and "@octocat" not in body
    assert "在庫 Closes #​82 @​octocat" in body  # 1 行にして、#・@ を無効にする


def test_link_models_picks_model_by_kind() -> None:
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
                }
            ]
        }
    )
    onto = apply(ONTOLOGY, patch)
    linked, names = link_models(
        onto, [("raw.inventory", "fct_inv", "event"), ("raw.inventory", "dim_inv", "entity")]
    )
    assert names == ["inventory"] and linked.concept("inventory").model == "fct_inv"
    # 同じ源泉の出来事と状態の概念は、fct の種類（出来事か定期スナップショットか）で選ぶ
    state = linked.concept("inventory")
    assert state is not None
    level = state.model_copy(update={"name": "stock_level", "kind": "state", "model": None})
    onto = onto.model_copy(update={"concepts": [*onto.concepts, level]})
    linked, names = link_models(onto, [("raw.inventory", "fct_stock_levels", "state")])
    assert names == ["stock_level"] and linked.concept("inventory").model is None


def test_link_models_keeps_one_concept_per_model() -> None:
    """再利用した既存の fct は、同じ源泉のほかの（モデルの無い）概念に書き込まない（1 対 1）。"""
    line = ONTOLOGY.concept("order_line")
    assert line is not None and line.model == "fct_order_lines"
    extra = line.model_copy(update={"name": "order_line_pipeline", "model": None})
    onto = ONTOLOGY.model_copy(update={"concepts": [*ONTOLOGY.concepts, extra]})
    linked, names = link_models(onto, [("raw.order_items", "fct_order_lines", "event")])
    assert names == [] and linked.concept("order_line_pipeline").model is None
    # 新しい名前の fct なら、モデルの無い概念に書き込む
    linked, names = link_models(onto, [("raw.order_items", "fct_order_pipelines", "event")])
    assert names == ["order_line_pipeline"]


def test_design_check_ignores_other_dimensions_of_the_same_source() -> None:
    """同じ源泉でもキーの違う切り口（店舗の地方など）は、概念の dim と別でよい。"""
    from adp.beam import load as load_beam

    if not (REPO / "warehouse/data/dev.duckdb").exists():
        pytest.skip("make setup が必要")
    beam = load_beam(REPO / "backend/tests/fixtures/beam/sales_by_store.yml")
    data = beam.model_dump(mode="json")
    data["events"][0]["dimensions"].append(
        {
            "w": "where",
            "name": "region",
            "label": "地方",
            "business_key": {"table": "raw.stores", "column": "region"},
            "fact_key": {"table": "raw.orders", "column": "store_id"},
            "attributes": [],
        }
    )
    with_region = type(beam).model_validate(data)
    db_catalog = Catalog(REPO / "warehouse/data/dev.duckdb")
    issues = DesignBuilder(CatalogSchema(db_catalog), ontology=ONTOLOGY).build(with_region).blockers
    assert not any("再利用してください" in i.message for i in issues), [i.message for i in issues]


def test_draft_api_error_codes(tmp_path: Path) -> None:
    """既に取り込み済みは 409、対象外のテーブルは 404（#87 レビュー）。"""
    from fastapi.testclient import TestClient

    from adp.api.app import create_app
    from adp.api.service import SessionService
    from adp.api.store import Store
    from adp.interview import Interviewer
    from adp.onboarding.pipeline import OnboardingPipeline
    from adp.pipeline import PipelineConfig

    db = REPO / "warehouse/data/dev.duckdb"
    if not db.exists():
        pytest.skip("make setup が必要")

    async def pipeline(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    onboarding = OnboardingPipeline(PipelineConfig(REPO, Path("."), db, None), None, LLMSettings())
    service = SessionService(Store(tmp_path / "app.db"), Interviewer(None, LLMSettings()), pipeline)  # type: ignore[arg-type]
    client = TestClient(create_app(service, onboarding=onboarding))
    for table, code in (("raw.orders", 409), ("orders", 409), ("raw.nope", 404), ("main.x", 404)):
        res = client.post(f"/sources/{table}/onboarding/draft", json={"agent": False})
        assert res.status_code == code, (table, res.text)


async def test_onboarding_is_not_repeated_without_redo(tmp_path: Path) -> None:
    """PR を作った取り込みは、明示しない限りもう一度作らない（同じ源泉の PR を 2 本作らない）。"""
    from adp.api.service import Conflict, SessionService
    from adp.api.store import Store
    from adp.interview import Interviewer
    from adp.pipeline import PipelineResult

    calls: list[str] = []

    async def onboard(source, patch, cleansing, on_step):  # type: ignore[no-untyped-def]
        calls.append(source)
        return PipelineResult(ok=True, pr_url="https://github.com/o/r/pull/9")

    async def pipeline(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    store = Store(tmp_path / "app.db")
    store.save_job("j0", "onboard:raw.inventory", "succeeded", [], "https://github.com/o/r/pull/8")
    service = SessionService(store, Interviewer(None, LLMSettings()), pipeline, onboard)  # type: ignore[arg-type]
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
                }
            ]
        }
    )
    with pytest.raises(Conflict, match="pull/8"):
        service.start_onboarding("raw.inventory", patch, "source: raw.inventory\n")
    job = service.start_onboarding("raw.inventory", patch, "source: raw.inventory\n", redo=True)
    assert job["status"] == "running"
    import asyncio

    await asyncio.gather(*service._tasks)
    assert calls == ["raw.inventory"]


def test_cleansing_spec_round_trips_through_the_form() -> None:
    """画面のフォームは定義を JSON で受け取って直し、確定のときは YAML にそろえる（#150）。

    フォームに出ない項目（rename・values・timezone・dedupe・exclude）も、そのまま残る。
    """
    from adp.onboarding.pipeline import _spec_or_none, cleansing_text, parse_cleansing

    text = (
        "source: raw.inventory\nkey: inventory_id\ncolumns:\n"
        "- name: movement_type\n  rename: kind\n  values: {RCV: receipt}\n"
        "  accepted_values: [receipt, shipment]\n"
        "- name: moved_at\n  cast: timestamp\n  timezone: [UTC, Asia/Tokyo]\n"
        "dedupe:\n  key: [inventory_id]\n  order_by: [{column: updated_at, desc: true}]\n"
        "exclude:\n- column: quantity\n  is_null: true\n"
    )
    spec = _spec_or_none(text, "raw.inventory")
    assert spec is not None
    form = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    # フォームで直す: 許す値を足し、日時から日付を作る
    form["columns"][0]["accepted_values"].append("adjustment")
    form["columns"][1]["derive"] = [{"name": "moved_on", "as": "date"}]
    back = parse_cleansing(cleansing_text(form), "raw.inventory")
    kind, moved = back.rule("movement_type"), back.rule("moved_at")
    assert kind is not None and moved is not None
    assert kind.accepted_values == ["receipt", "shipment", "adjustment"]
    assert (kind.rename, kind.values) == ("kind", {"RCV": "receipt"})
    assert moved.timezone == ("UTC", "Asia/Tokyo")
    assert moved.derive[0].as_ == "date"
    assert back.dedupe == spec.dedupe and back.exclude == spec.exclude
    # 読めない定義はフォームに出さない（YAML で直す）。YAML はそのまま通す
    assert _spec_or_none("source: [", "raw.inventory") is None
    assert _spec_or_none("source: raw.orders\n", "raw.inventory") is None
    assert cleansing_text("source: raw.inventory\n") == "source: raw.inventory\n"
    with pytest.raises(ValueError):
        cleansing_text({"source": "raw.inventory", "columns": [{"name": "x", "cast": "nope"}]})
    with pytest.raises(ValueError, match="長すぎ"):
        cleansing_text({"source": "raw.inventory", "description": "あ" * 20001})


def test_date_derives_follow_the_naming_convention() -> None:
    """日時から作る日付の列は _on（規約）。ほかの出力の列と重なる名前は出さない（#150）。"""
    from adp.cleansing.check import column_kind, column_kinds, date_derives
    from adp.cleansing.models import CleansingSpec
    from adp.cleansing.render import SourceColumn

    columns = [
        SourceColumn("ordered_at", "TIMESTAMP"),
        SourceColumn("shipped_at", "VARCHAR"),
        SourceColumn("shipped_on", "DATE"),
        SourceColumn("loaded_at", "TIMESTAMP"),
        SourceColumn("note", "VARCHAR"),
        SourceColumn("paid_at", "TIMESTAMP"),
        SourceColumn("paid", "TIMESTAMP"),
        SourceColumn("tags_at", "TIMESTAMP[]"),
    ]
    spec = CleansingSpec.model_validate(
        {
            "source": "raw.orders",
            "columns": [
                {"name": "shipped_at", "cast": "timestamp"},
                {"name": "loaded_at", "drop": True},
                {"name": "ordered_at", "derive": [{"name": "order_day", "as": "date"}]},
            ],
        }
    )
    # 作ってある日付はその名前、型変換で日時になる列も対象、重なる名前は None、落とす列は出さない
    # 候補どうしで重なる名前（paid_at・paid → paid_on）も None。配列は日時として扱わない
    assert date_derives(spec, columns) == {
        "ordered_at": "order_day",
        "shipped_at": None,
        "paid_at": None,
        "paid": None,
    }
    assert column_kinds(spec, columns)["shipped_at"] == "timestamp"  # 型変換の後の型
    assert [
        column_kind(t)
        for t in (
            "VARCHAR",
            "TIMESTAMP WITH TIME ZONE",
            "DATE",
            "BIGINT",
            "DECIMAL(10,2)",
            "BOOLEAN",
            "BLOB",
            "INTERVAL",
            "INTEGER[]",
        )
    ] == [
        "text",
        "timestamp",
        "date",
        "number",
        "number",
        "boolean",
        "other",
        "other",
        "other",
    ]


def test_onboarding_api_rejects_invalid_cleansing_json(tmp_path: Path) -> None:
    """フォームから送った定義（JSON）が形式に合わなければ 422 で返す（#150）。"""
    from fastapi.testclient import TestClient

    from adp.api.app import create_app
    from adp.api.service import SessionService
    from adp.api.store import Store
    from adp.interview import Interviewer
    from adp.onboarding.pipeline import OnboardingPipeline
    from adp.pipeline import PipelineConfig

    db = REPO / "warehouse/data/dev.duckdb"
    if not db.exists():
        pytest.skip("make setup が必要")

    async def pipeline(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    onboarding = OnboardingPipeline(PipelineConfig(REPO, Path("."), db, None), None, LLMSettings())
    service = SessionService(Store(tmp_path / "app.db"), Interviewer(None, LLMSettings()), pipeline)  # type: ignore[arg-type]
    client = TestClient(create_app(service, onboarding=onboarding))
    body = {
        "patch": {
            "concepts": [
                {
                    "name": "review",
                    "label": "レビュー",
                    "domain": "channel",
                    "kind": "event",
                    "source": "raw.reviews",
                    "key": "review_id",
                }
            ]
        },
        "cleansing": {"source": "raw.reviews", "columns": [{"name": "rating", "cast": "nope"}]},
    }
    res = client.post("/sources/raw.reviews/onboarding", json=body)
    assert res.status_code == 422, res.text
    assert "cast" in res.text
    # 別の源泉の定義も 422
    body["cleansing"] = {"source": "raw.orders"}
    res = client.post("/sources/raw.reviews/onboarding", json=body)
    assert res.status_code == 422, res.text
    assert "raw.orders" in res.text


def test_patch_must_not_duplicate_existing_labels(catalog: Catalog) -> None:
    """既存の属性と同じラベルは業務の言葉の二重定義（#137）。決定的な案は概念の名前で分け、
    エージェントの案なら誤りにする。"""
    profile = catalog.profile_table("raw.inventory")
    profile = profile.model_copy(
        update={
            "description": "在庫の入出庫",
            "columns": [
                c.model_copy(update={"description": "販売数量"}) if c.name == "quantity" else c
                for c in profile.columns
            ],
        }
    )
    patch = propose_from_profile(profile, ONTOLOGY)
    labels = {a.column: a.label for a in patch.concepts[0].attributes}
    assert labels["quantity"] == "在庫の入出庫の販売数量"
    assert validate_patch(ONTOLOGY, patch, catalog, "raw.inventory") == []
    data = patch.model_dump(mode="json", by_alias=True)
    for a in data["concepts"][0]["attributes"]:
        if a["column"] == "quantity":
            a["label"] = "販売数量"
    errors = validate_patch(ONTOLOGY, OntologyPatch.model_validate(data), catalog, "raw.inventory")
    assert [e for e in errors if e.startswith("ラベル「販売数量」")], errors
