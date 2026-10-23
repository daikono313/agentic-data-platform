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


def test_unavailable_becomes_warning(design: PhysicalDesign) -> None:
    refs = {i.ref for i in design.issues if i.severity == IssueSeverity.WARNING}
    assert {"gross_margin", "customer.annual_income"} <= refs


def test_history_required_becomes_history_model(design: PhysicalDesign) -> None:
    """当時の値で見たい属性は、snapshot と履歴モデルを作る（警告にしない。ADR 0009）。"""
    customer = next(d for d in design.dims if d.name == "dim_customer")
    assert customer.history is not None
    assert customer.history.model == "dim_customer_history"
    assert customer.history.snapshot == "snp_customers"
    assert customer.history.natural_key == "customer_id"
    assert [(c.name, c.expr) for c in customer.history.columns] == [
        ("membership_rank", "membership_rank")
    ]
    refs = {i.ref for i in design.issues if i.severity == IssueSeverity.WARNING}
    assert "customer.membership_rank" not in refs
    # 履歴の要る属性が無い dim には作らない
    assert all(d.history is None for d in design.dims if d.name != "dim_customer")


def test_history_from_joined_table_or_expr_is_a_warning(beam: BeamModel) -> None:
    """源泉の表の列でない属性（結合した表・式）の履歴は作らず、警告にする（ADR 0009）。"""
    from adp.beam import SourceRef

    attr = next(
        a for e in beam.events for d in e.dimensions for a in d.attributes if a.history_required
    )
    attr.source = SourceRef(table=attr.source.table, expr=f"upper({attr.source.column})")  # type: ignore[union-attr]
    design = DesignBuilder(FakeSchema()).build(beam)
    customer = next(d for d in design.dims if d.name == "dim_customer")
    assert customer.history is None
    assert any("履歴は作らず" in i.message for i in design.issues)


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


def test_er_diagram_labels_are_escaped_like_ontology(design: PhysicalDesign) -> None:
    """ER 図のラベルもオントロジーの図と同じく、構文・設定・コメントの文字を無効にする（#110）。"""
    design = design.model_copy(deep=True)
    design.dims[0].label = 'a "b" %%{init: {"theme":"dark"}}%% `c`\nd'
    er = er_diagram(design)
    line = next(x for x in er.splitlines() if " PK " in x and "#quot;" in x)
    label = line.split(" PK ", 1)[1]
    assert label.startswith('"a #quot;b#quot;') and label.endswith(' d"')
    assert "%" not in label and "{" not in label and "`" not in label


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


def test_duplicate_columns_are_blockers(beam: BeamModel) -> None:
    event = beam.events[0]
    event.measures[0] = event.measures[0].model_copy(update={"name": "order_id"})
    design = DesignBuilder(FakeSchema()).build(beam)
    assert any("order_id が重複" in i.message for i in design.blockers)


def test_periodic_snapshot_design(beam: BeamModel) -> None:
    """定期的な状態の fct は、区切り × 期間の終わりの日付と、値の求め方を持つ（ADR 0008）。"""
    from adp.beam import MetricFilter, StateMethod, StateSpec, StoryType, TimeGrain
    from adp.design.builder import SNAPSHOT_TIME_COLUMN

    event = beam.events[0]
    event.story_type = StoryType.RECURRING
    # 記録 1 件の番号を粒度にした状態は作れない
    design = DesignBuilder(FakeSchema()).build(beam)
    assert any("区切り（店舗 × 商品など）× 時点" in i.message for i in design.blockers)

    event.grain_keys = ["store", "product", "date"]
    event.dimensions[[d.ref for d in event.dimensions].index("date")].time_grain = TimeGrain.MONTH
    for m in event.measures:
        m.state = StateSpec(method=StateMethod.LAST_VALUE)
    quantity = event.measure("quantity")
    assert quantity is not None
    reset = {"ref": "order_status.order_status", "operator": "in", "values": ["completed"]}
    quantity.state = StateSpec(
        method=StateMethod.RUNNING_TOTAL, reset=[MetricFilter.model_validate(reset)]
    )
    # 区分（注文ステータス）は記録 1 件ごとの項目なので、状態の指標の絞り込みには使えない
    design = DesignBuilder(FakeSchema()).build(beam)
    assert any("記録 1 件ごとの項目" in i.message for i in design.blockers)
    for metric in beam.metrics:
        metric.filters = []
    design = DesignBuilder(FakeSchema()).build(beam)
    assert design.blockers == []
    [fact] = design.facts
    assert fact.snapshot is not None
    assert fact.grain_columns == ["store_key", "product_key", SNAPSHOT_TIME_COLUMN]
    assert fact.agg_time_column == SNAPSHOT_TIME_COLUMN
    assert fact.snapshot.grain == TimeGrain.MONTH
    assert fact.snapshot.recorded_at == "t1.ordered_at"
    # 記録 1 件ごとの列（日時・注文番号・区分）は持たない。区分は置き換える行の条件にだけ使う
    names = [c.name for c in fact.columns]
    assert "ordered_at" not in names and "order_id" not in names and "order_status" not in names
    assert fact.snapshot.methods["quantity"] == StateMethod.RUNNING_TOTAL
    assert fact.snapshot.resets == {"quantity": "(t1.order_status in ('completed'))"}

    # 時点（When）が 2 つあると、どちらの時点の状態か決まらない
    date = next(d for d in event.dimensions if d.ref == "date")
    event.dimensions.append(date.model_copy(update={"name": "shipped", "role": None}))
    design = DesignBuilder(FakeSchema()).build(beam)
    assert any("時点（When）を 1 つ" in i.message for i in design.blockers)


def test_metric_group_by_must_exist_in_design(beam: BeamModel) -> None:
    beam.metrics[0].group_by = ["customer.annual_income"]
    design = DesignBuilder(FakeSchema()).build(beam)
    assert any("customer.annual_income" in (i.ref or "") for i in design.blockers)
