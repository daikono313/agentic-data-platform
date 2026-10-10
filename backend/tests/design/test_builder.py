import importlib.util
import sys
from pathlib import Path

import pytest

from adp.beam import BeamModel, load
from adp.catalog.catalog import Catalog
from adp.design import (
    ColumnRole,
    DesignBuilder,
    ForeignKey,
    IssueSeverity,
    PhysicalDesign,
    er_diagram,
)
from adp.design.catalog_schema import CatalogSchema

FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "sales_by_store.yml"
SCRIPT = Path(__file__).parents[3] / "warehouse" / "scripts" / "generate_sample_data.py"


class FakeSchema:
    """サンプルデータの raw スキーマの縮小版。"""

    COLUMNS = {
        "raw.order_items": [
            "order_item_id",
            "order_id",
            "product_id",
            "quantity",
            "unit_price",
            "discount_amount",
        ],
        "raw.orders": [
            "order_id",
            "customer_id",
            "store_id",
            "ordered_at",
            "promotion_id",
            "order_status",
            "payment_method",
        ],
        "raw.customers": ["customer_id", "membership_rank", "region", "prefecture"],
        "raw.products": ["product_id", "category", "subcategory"],
        "raw.stores": ["store_id", "store_name", "channel"],
        "raw.promotions": ["promotion_id", "promotion_name"],
    }
    FKS = {
        "raw.order_items": [
            ForeignKey("order_id", "raw.orders", "order_id"),
            ForeignKey("product_id", "raw.products", "product_id"),
        ],
        "raw.orders": [
            ForeignKey("customer_id", "raw.customers", "customer_id"),
            ForeignKey("store_id", "raw.stores", "store_id"),
            ForeignKey("promotion_id", "raw.promotions", "promotion_id"),
        ],
    }

    def columns(self, table: str) -> list[str]:
        return self.COLUMNS.get(table, [])

    def foreign_keys(self, table: str) -> list[ForeignKey]:
        return self.FKS.get(table, [])


@pytest.fixture
def beam() -> BeamModel:
    return load(FIXTURE)


@pytest.fixture
def design(beam: BeamModel) -> PhysicalDesign:
    return DesignBuilder(FakeSchema()).build(beam)


def test_fact_follows_conventions(design: PhysicalDesign) -> None:
    assert design.blockers == []
    fact = design.facts[0]
    assert fact.name == "fct_order_lines"
    assert fact.primary_entity == "order_line"
    assert fact.grain_columns == ["order_line_id"]
    assert fact.base.model == "stg_order_items" and fact.base.alias == "t0"
    assert [(j.left_alias, j.left_column, j.right.model, j.right_column) for j in fact.joins] == [
        ("t0", "order_id", "stg_orders", "order_id")
    ]
    cols = {c.name: c for c in fact.columns}
    assert cols["order_line_id"].expr == "t0.order_item_id"
    assert cols["order_id"].expr == "t1.order_id"
    assert cols["customer_key"].expr == "{{ dim_key('t1.customer_id') }}"
    assert cols["customer_key"].references == "dim_customer.customer_key"
    assert cols["product_key"].expr == "{{ dim_key('t0.product_id') }}"
    assert cols["ordered_on"].expr == "cast(t1.ordered_at as date)"
    assert cols["ordered_on"].references == "dim_date.date_day"
    assert cols["ordered_at"].role == ColumnRole.TIMESTAMP
    # マスタなしのディメンションは dim を作らず、ref 名の列にする
    assert cols["payment_method"].role == ColumnRole.DEGENERATE
    assert cols["payment_method"].expr == "t1.payment_method"
    # measure の expr は列を別名で修飾する
    assert cols["sales_amount"].expr == "t0.quantity * t0.unit_price - t0.discount_amount"
    assert "gross_margin" not in cols
    assert fact.agg_time_column == "ordered_on"


def test_dims_follow_conventions(design: PhysicalDesign) -> None:
    dims = {d.name: d for d in design.dims}
    assert set(dims) == {"dim_customer", "dim_product", "dim_store", "dim_promotion"}
    customer = dims["dim_customer"]
    assert customer.key_column == "customer_key"
    assert [c.name for c in customer.columns] == [
        "customer_id",
        "membership_rank",
        "region",
        "prefecture",
    ]
    assert customer.columns[0].role == ColumnRole.NATURAL_KEY
    assert customer.hierarchy == ["region", "prefecture"]
    assert design.uses_dim_date


def test_unavailable_and_history_become_warnings(design: PhysicalDesign) -> None:
    refs = {i.ref for i in design.issues if i.severity == IssueSeverity.WARNING}
    assert {"gross_margin", "customer.annual_income", "customer.membership_rank"} <= refs


def test_missing_join_path_is_a_blocker(beam: BeamModel) -> None:
    class NoFks(FakeSchema):
        FKS: dict[str, list[ForeignKey]] = {}

    design = DesignBuilder(NoFks()).build(beam)
    assert any("raw.orders" in i.message for i in design.blockers)


def test_existing_dim_reports_new_columns(beam: BeamModel) -> None:
    existing = {"dim_customer": {"customer_key", "customer_id", "region", "prefecture"}}
    design = DesignBuilder(FakeSchema(), existing).build(beam)
    customer = next(d for d in design.dims if d.name == "dim_customer")
    assert customer.exists and customer.new_columns == ["membership_rank"]
    store = next(d for d in design.dims if d.name == "dim_store")
    assert not store.exists and store.new_columns == []


def test_er_diagram(design: PhysicalDesign) -> None:
    er = er_diagram(design)
    assert er.startswith("erDiagram")
    assert 'dim_customer ||--o{ fct_order_lines : "customer_key"' in er
    assert 'dim_date ||--o{ fct_order_lines : "ordered_on"' in er
    assert "order_line_id PK" in er


def test_with_real_catalog(beam: BeamModel, tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("generate_sample_data", SCRIPT)
    assert spec and spec.loader
    gen = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gen
    spec.loader.exec_module(gen)
    db = tmp_path / "t.duckdb"
    gen.load(db, gen.generate(300, 1))
    design = DesignBuilder(CatalogSchema(Catalog(db))).build(beam)
    assert design.blockers == []
    assert [j.right.model for j in design.facts[0].joins] == ["stg_orders"]


def test_order_status_is_available_for_filters(design: PhysicalDesign) -> None:
    cols = {c.name: c for c in design.facts[0].columns}
    assert cols["order_status"].expr == "t1.order_status"


class MultiPath(FakeSchema):
    COLUMNS = {
        **FakeSchema.COLUMNS,
        "raw.customers": [*FakeSchema.COLUMNS["raw.customers"], "region_id"],
        "raw.stores": [*FakeSchema.COLUMNS["raw.stores"], "region_id"],
        "raw.regions": ["region_id", "region_name"],
    }
    FKS = {
        **FakeSchema.FKS,
        "raw.customers": [ForeignKey("region_id", "raw.regions", "region_id")],
        "raw.stores": [ForeignKey("region_id", "raw.regions", "region_id")],
    }


def test_ambiguous_join_path_is_a_blocker(beam: BeamModel) -> None:
    from adp.beam import DegenerateDimension, SourceRef

    beam.events[0].degenerate_dimensions.append(
        DegenerateDimension(
            name="region_name",
            label="地域",
            source=SourceRef(table="raw.regions", column="region_name"),
        )
    )
    design = DesignBuilder(MultiPath()).build(beam)
    assert any("経路が複数" in i.message for i in design.blockers)


def test_expr_qualification_edge_cases() -> None:
    from adp.beam import SourceRef
    from adp.design.builder import JoinError, _Sources

    src = _Sources("raw.orders", FakeSchema())
    assert (
        src.expr(
            SourceRef(table="raw.orders", expr="case when order_status = 'order_status' then 1 end")
        )
        == "case when t0.order_status = 'order_status' then 1 end"
    )
    assert src.expr(SourceRef(table="raw.orders", expr="cast(ordered_at as date)")) == (
        "cast(t0.ordered_at as date)"
    )
    src.alias("raw.customers")
    with pytest.raises(JoinError, match="以外の列"):
        src.expr(SourceRef(table="raw.orders", expr="order_id + membership_rank"))


def test_dim_key_with_quotes() -> None:
    from adp.design.builder import dim_key_expr

    assert dim_key_expr("coalesce(t0.store_id, 'web')") == (
        "{{ dim_key(\"coalesce(t0.store_id, 'web')\") }}"
    )


def test_base_table_is_the_finest_regardless_of_order(beam: BeamModel) -> None:
    from adp.beam import DegenerateDimension, SourceRef

    event = beam.events[0]
    event.degenerate_dimensions = [
        DegenerateDimension(
            name="order_id",
            label="注文番号",
            source=SourceRef(table="raw.orders", column="order_id"),
        ),
        *[d for d in event.degenerate_dimensions if d.name != "order_id"],
    ]
    event.grain_keys = ["order_id", "order_line_id"]
    design = DesignBuilder(FakeSchema()).build(beam)
    assert design.blockers == []
    assert design.facts[0].base.table == "raw.order_items"


def test_duplicate_columns_and_periodic_snapshot_are_blockers(beam: BeamModel) -> None:
    from adp.beam import StoryType

    event = beam.events[0]
    event.measures[0] = event.measures[0].model_copy(update={"name": "order_id"})
    design = DesignBuilder(FakeSchema()).build(beam)
    assert any("order_id が重複" in i.message for i in design.blockers)

    event.story_type = StoryType.RECURRING
    design = DesignBuilder(FakeSchema()).build(beam)
    assert any("定期スナップショット" in i.message for i in design.blockers)


def test_metric_group_by_must_exist_in_design(beam: BeamModel) -> None:
    beam.metrics[0].group_by = ["customer.annual_income"]
    design = DesignBuilder(FakeSchema()).build(beam)
    assert any("customer.annual_income" in (i.ref or "") for i in design.blockers)
