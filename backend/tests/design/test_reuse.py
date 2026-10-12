"""新しい指標は「既存の fact → データ全体 → 新規作成」の順に（#113）。用途の重複する fact を作らない

#111 の完了条件 3: 既存の fact で作れる要求では新しい fact を作らず、データ全体にしかない要求では
新しいクレンジング層と fact を作る。オントロジーに無い源泉からは作らない（#109）。
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from adp.beam import BeamModel, FactType, load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.design.check import existing_facts, load_ontology
from adp.design.models import ExistingFact
from adp.generation import generate_models
from adp.generation.project import ProjectInfo
from adp.llm.settings import REPO_ROOT
from adp.ontology import Ontology
from tests.design.test_builder import FakeSchema

FIXTURES = Path(__file__).parents[1] / "fixtures" / "beam"
WAREHOUSE = REPO_ROOT / "warehouse"
DB = WAREHOUSE / "data" / "dev.duckdb"
needs_db = pytest.mark.skipif(not DB.exists(), reason="make setup が必要")


def _design(beam: BeamModel):  # type: ignore[no-untyped-def]
    catalog = Catalog(DB)
    builder = DesignBuilder(
        CatalogSchema(catalog), ontology=load_ontology(REPO_ROOT / "ontology" / "ontology.yml")
    )
    return builder.build(beam), ProjectInfo(WAREHOUSE, catalog)


@needs_db
def test_existing_fact_is_reused_not_duplicated() -> None:
    """既存の fct_order_lines で作れる要求は、新しい fact を作らずに既存の fact に足す。"""
    beam = load(FIXTURES / "sales_by_store.yml")
    design, info = _design(beam)
    assert not design.blockers, [b.message for b in design.blockers]
    assert [f.name for f in design.facts] == ["fct_order_lines"]
    generation = generate_models(design, info, beam)
    # 新しい fct は作らず、既存の fct_order_lines の YAML（Semantic Layer）に指標を足す
    files = {f.path: f for f in generation.files}
    assert not any(
        p.startswith("models/marts/fct_") and p.endswith(".sql") and files[p].replaces is None
        for p in files
    )
    assert files["models/marts/fct_order_lines.yml"].replaces is not None


@needs_db
def test_reused_fact_does_not_duplicate_a_column_with_the_same_source() -> None:
    """既存の列と同じ源泉の列（別名の measure）を足さず、既存の列で指標を作る（#113）。"""
    data = load(REPO_ROOT / "beam" / "inventory_status.yml").model_dump(mode="json")
    event = data["events"][0]
    same = next(m for m in event["measures"] if m["name"] == "movement_qty")
    event["measures"].append({**same, "name": "moved_quantity", "label": "動いた数量"})
    data["metrics"] = [
        {
            "name": "moved_quantity_total",
            "label": "動いた数量",
            "question": "月別の動いた数量",
            "framed_question": "月別の動いた数量はいくつか",
            "type": "simple",
            "event": event["name"],
            "measure": "moved_quantity",
            "group_by": ["moved_at.month"],
            "status": "confirmed",
        }
    ]
    beam = BeamModel.model_validate(data)
    design, info = _design(beam)
    assert not design.blockers, [b.message for b in design.blockers]
    files = {f.path: f for f in generate_models(design, info, beam).files}
    assert "models/marts/fct_stock_movements.sql" not in files  # 列を足さない
    yml = files["models/marts/fct_stock_movements.yml"].content
    metric = next(
        m
        for m in yaml.safe_load(yml)["models"][0]["metrics"]
        if m["name"] == "moved_quantity_total"
    )
    assert metric["expr"] == "movement_qty"


@needs_db
def test_same_expression_on_a_joined_table_is_not_reused() -> None:
    """結合先（t1 など）の列は、既存の fct と新しい設計で別のテーブルでありうるので再利用しない。"""
    data = load(FIXTURES / "sales_by_store.yml").model_dump(mode="json")
    event = data["events"][0]
    event["measures"].append(
        {
            "name": "order_ref",
            "label": "注文番号の合計",
            "aggregation": "sum",
            "source": {"table": "raw.orders", "column": "order_id"},
            "availability": "available",
        }
    )
    data["metrics"].append(
        {
            **data["metrics"][0],
            "name": "order_ref_total",
            "measure": "order_ref",
            "filters": [],
            "filter": None,
        }
    )
    beam = BeamModel.model_validate(data)
    design, info = _design(beam)
    assert not design.blockers, [b.message for b in design.blockers]
    assert next(c for c in design.facts[0].columns if c.name == "order_ref").expr == "t1.order_id"

    class Injected(ProjectInfo):
        """既存の fct に、同じ式（t1.order_id）の別の列があるとする。"""

        def model_file(self, model: str, suffix: str) -> tuple[str, str] | None:
            found = super().model_file(model, suffix)
            if found is None or model != "fct_order_lines":
                return found
            path, text = found
            if suffix == ".sql":
                return path, text.replace(
                    "    t0.quantity as quantity\n",
                    "    t0.quantity as quantity,\n    t1.order_id as order_ref_old\n",
                )
            return path, text.replace(
                "  - name: quantity\n",
                "  - name: order_ref_old\n    description: x\n  - name: quantity\n",
            )

    generation = generate_models(design, Injected(info.warehouse_dir, info.catalog), beam)
    assert "order_ref_old" not in "".join(f.content for f in generation.files)
    assert any(t.kind == "fact_exists" and t.target == "fct_order_lines" for t in generation.tasks)


@needs_db
def test_same_event_under_another_name_is_blocked() -> None:
    """同じ源泉の出来事を別の名前の fact にしようとしたら止め、既存の fact を案内する。"""
    beam = load(FIXTURES / "sales_by_store.yml")
    data = beam.model_dump(mode="json")
    data["events"][0]["table_name"] = "sales_lines"
    design, _ = _design(BeamModel.model_validate(data))
    messages = " / ".join(b.message for b in design.blockers)
    assert "fct_order_lines" in messages and "用途が同じ fact" in messages

    # 在庫は fct_stock_movements として取り込み済み。別の名前（fct_inventory_movements）は止める
    design, _ = _design(load(FIXTURES / "inventory_receipts.yml"))
    assert any("fct_stock_movements" in b.message for b in design.blockers)


@needs_db
def test_data_only_in_sources_creates_cleansing_and_fact() -> None:
    """既存の fact に無く、データ全体（オントロジーの出来事）にある要求は、stg と fact を作る。"""
    beam = load(FIXTURES / "payments_by_method.yml")
    design, info = _design(beam)
    assert not design.blockers, [b.message for b in design.blockers]
    paths = {f.path for f in generate_models(design, info, beam).files}
    assert "models/marts/fct_payments.sql" in paths
    assert "models/staging/stg_payments.sql" in paths
    assert "cleansing/raw.payments.yml" in paths


@needs_db
def test_unmapped_source_is_blocked() -> None:
    """オントロジーに無い（未整理の）源泉からは作らず、取り込みを案内する（#109）。"""
    beam = load(FIXTURES / "payments_by_method.yml")
    catalog = Catalog(DB)
    ontology = load_ontology(REPO_ROOT / "ontology" / "ontology.yml")
    assert ontology is not None
    ontology = ontology.model_copy(
        update={"concepts": [c for c in ontology.concepts if c.source != "raw.payments"]}
    )
    design = DesignBuilder(CatalogSchema(catalog), ontology=ontology).build(beam)
    assert any(
        "未整理" in b.message and "取り込む" in b.message and "マージ" in b.message
        for b in design.blockers
    )


# --- 設計の確認（DB なし。FakeSchema とオントロジーの写し） --------------------------------


def _ontology(**update: Any) -> Ontology:
    ontology = load_ontology(REPO_ROOT / "ontology" / "ontology.yml")
    assert ontology is not None
    return ontology.model_copy(update=update) if update else ontology


def _messages(beam: BeamModel, ontology: Ontology, **kw: Any) -> list[str]:
    design = DesignBuilder(FakeSchema(), ontology=ontology, **kw).build(beam)
    return [b.message for b in design.blockers]


def _without(*sources: str) -> Ontology:
    o = _ontology()
    return _ontology(concepts=[c for c in o.concepts if c.source not in sources])


def test_unmapped_dim_source_names_the_dim_once() -> None:
    """dim の源泉が未整理なら、dim の名前（dim_store。dim_dim_store ではない）で止める。"""
    messages = _messages(load(FIXTURES / "sales_by_store.yml"), _without("raw.stores"))
    assert any(m.startswith("dim_store の源泉 raw.stores") for m in messages), messages
    assert not any("dim_dim_" in m for m in messages)


def test_unmapped_join_source_is_blocked() -> None:
    """結合するテーブル（fct_order_lines の raw.orders）が未整理でも止める（stg を作るので）。"""
    messages = _messages(load(FIXTURES / "sales_by_store.yml"), _without("raw.orders"))
    assert any(
        m.startswith("fct_order_lines の結合する源泉 raw.orders") and "未整理" in m
        for m in messages
    ), messages


def _renamed(story_type: str | None = None, table_name: str = "order_pipelines") -> BeamModel:
    data = load(FIXTURES / "sales_by_store.yml").model_dump(mode="json")
    data["events"][0]["table_name"] = table_name
    if story_type:
        data["events"][0]["story_type"] = story_type
    return BeamModel.model_validate(data)


GRAIN = frozenset({"raw.order_items.order_item_id"})
"""sales_by_store の粒度のキーの源泉。"""


def _known(fact_type: FactType = FactType.TRANSACTION, **kw: Any) -> dict[str, ExistingFact]:
    return {"fct_order_lines": ExistingFact(fact_type=fact_type, grain_sources=GRAIN, **kw)}


def test_duplicate_fact_of_the_same_type_points_to_the_existing_fact() -> None:
    messages = _messages(_renamed(), _ontology(), existing_facts=_known())
    assert any("table_name を order_lines" in m for m in messages), messages
    # 名前も同じなら止めない（既存の fact に指標を足す）
    assert _messages(_renamed(table_name="order_lines"), _ontology(), existing_facts=_known()) == []


def test_duplicate_fact_of_another_type_needs_its_own_concept() -> None:
    """種類の違う fact（累積スナップショット）は、先にその fact 用の概念を足すよう案内する。"""
    messages = _messages(_renamed("evolving"), _ontology(), existing_facts=_known())
    dup = [m for m in messages if "fct_order_pipelines" in m and "fct_order_lines" in m]
    assert dup and "種類・粒度の違う" in dup[0] and "概念をオントロジーに追加" in dup[0], messages
    assert "種類（transaction と accumulating_snapshot）" in dup[0]
    assert "table_name を" not in dup[0]
    # その fact 用の（モデルの無い）出来事の概念が足してあれば、止めない
    o = _ontology()
    line = next(c for c in o.concepts if c.source == "raw.order_items")
    extra = line.model_copy(update={"name": "order_line_pipeline", "model": None})
    messages = _messages(
        _renamed("evolving"),
        _ontology(concepts=[*o.concepts, extra]),
        existing_facts=_known(),
    )
    assert not any("fct_order_pipelines の源泉" in m for m in messages), messages


def test_same_name_with_another_type_or_grain_is_blocked() -> None:
    """名前が同じでも、既存の fct と種類・粒度が違えば止める（既存の fct は変えられない）。"""
    same_name = _renamed("evolving", table_name="order_lines")
    messages = _messages(same_name, _ontology(), existing_facts=_known())
    assert any(
        m.startswith("既存の fct_order_lines と、この要求の fact の種類") for m in messages
    ), messages
    other_grain = {
        "fct_order_lines": ExistingFact(
            fact_type=FactType.TRANSACTION, grain_sources=frozenset({"raw.orders.order_id"})
        )
    }
    same_grain_name = _renamed(table_name="order_lines")
    messages = _messages(same_grain_name, _ontology(), existing_facts=other_grain)
    assert any("粒度のキー（raw.orders.order_id と" in m for m in messages), messages


def test_unknown_or_conflicting_existing_fact_is_surfaced() -> None:
    """既存の fct の種類が BEAM 成果物から分からない・食い違うときは、確かめるよう課題に出す。"""
    beam = _renamed()
    [message] = _messages(beam, _ontology(), existing_facts={})
    assert "種類・粒度を確かめられません" in message and "beam/" in message
    assert "table_name を order_lines" in message and "概念をオントロジーに追加" in message
    # 名前が同じなら止めず、PR で確かめる警告にする
    design = DesignBuilder(FakeSchema(), ontology=_ontology(), existing_facts={}).build(
        _renamed(table_name="order_lines")
    )
    assert not design.blockers
    assert any("確かめられません" in i.message for i in design.issues)
    conflict = _known(conflict="BEAM 成果物 a と b で種類・粒度が食い違います")
    conflict["fct_order_lines"].fact_type = None
    [message] = _messages(beam, _ontology(), existing_facts=conflict)
    assert "a と b で種類・粒度が食い違います" in message


def test_two_events_from_the_same_source_in_one_beam_are_blocked() -> None:
    data = load(FIXTURES / "sales_by_store.yml").model_dump(mode="json")
    second = {**data["events"][0], "name": "customer_orders_product_again", "table_name": "x"}
    data["events"].append(second)
    messages = _messages(BeamModel.model_validate(data), _ontology())
    assert any("同じ源泉 raw.order_items から、複数の fact" in m for m in messages), messages


def test_existing_facts_from_beam_files(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    known = existing_facts(REPO_ROOT / "beam")["fct_stock_movements"]
    assert known.fact_type == FactType.TRANSACTION
    assert known.grain_sources == {"raw.inventory.inventory_id"}
    (tmp_path / "broken.yml").write_text("events: [", encoding="utf-8")
    assert existing_facts(tmp_path) == {}
    assert "broken.yml" in caplog.text  # 壊れた成果物はログに出す
    assert existing_facts(tmp_path / "none") == {}
    # BEAM 成果物どうしで種類が食い違えば、どちらかを選ばずに食い違いとして返す
    beam = load(FIXTURES / "sales_by_store.yml")
    other = _renamed("evolving", table_name="order_lines").model_copy(update={"name": "other"})
    from adp.beam import save

    save(beam, tmp_path / "a.yml")
    save(other, tmp_path / "b.yml")
    known = existing_facts(tmp_path)["fct_order_lines"]
    assert known.fact_type is None and known.conflict and "食い違います" in known.conflict


def _as_kind(source: str, kind: str) -> Ontology:
    """source の概念を別の種類（モデルなし）にしたオントロジー。"""
    o = _ontology()
    return _ontology(
        concepts=[
            c.model_copy(update={"kind": kind, "model": None}) if c.source == source else c
            for c in o.concepts
        ]
    )


def test_fact_without_an_event_concept_is_blocked() -> None:
    """もの・人の概念しか無い源泉から fct を作ると、どの概念にも結び付かず CI で落ちる。
    確定前に止める（#114）。"""
    messages = _messages(_renamed(), _as_kind("raw.order_items", "entity"))
    assert any(
        m.startswith("fct_order_pipelines を結び付けられるオントロジーの概念がありません")
        and "1 対 1" in m
        for m in messages
    ), messages


def test_dim_without_an_entity_concept_is_blocked() -> None:
    """出来事の概念しか無い源泉から dim を作るのも止める（#114）。"""
    messages = _messages(load(FIXTURES / "sales_by_store.yml"), _as_kind("raw.stores", "event"))
    assert any(m.startswith("dim_store を結び付けられる") for m in messages), messages


def test_linkable_models_are_not_blocked_twice() -> None:
    """ほかの検査が止めたモデル（未整理の源泉）は、結び付かないことを重ねて出さない。"""
    messages = _messages(load(FIXTURES / "sales_by_store.yml"), _without("raw.stores"))
    assert not any("結び付けられる" in m for m in messages), messages
