import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from adp.beam import (
    Availability,
    BeamModel,
    FactType,
    Gap,
    GapKind,
    MetricRequest,
    MetricType,
    SourceRef,
    StoryType,
    build_event_matrix,
    from_yaml,
    load,
    save,
    to_yaml,
)
from adp.beam.schema import json_schema

FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "sales_by_store.yml"


@pytest.fixture
def model() -> BeamModel:
    return load(FIXTURE)


def test_fixture_is_finalizable(model: BeamModel) -> None:
    assert model.finalize_issues() == []
    model.finalize()
    assert model.finalized_at is not None


def test_yaml_roundtrip(model: BeamModel, tmp_path: Path) -> None:
    path = tmp_path / "x.yml"
    save(model, path)
    assert load(path) == model
    assert from_yaml(to_yaml(model)) == model


def test_fact_table_name(model: BeamModel) -> None:
    assert model.events[0].fact_table == "fct_order_lines"


def test_fact_type_from_story(model: BeamModel) -> None:
    event = model.events[0]
    assert event.fact_type == FactType.TRANSACTION
    evolving = event.model_copy(update={"story_type": StoryType.EVOLVING})
    assert evolving.fact_type == FactType.ACCUMULATING_SNAPSHOT


def test_identifier_must_be_snake_case() -> None:
    with pytest.raises(ValidationError):
        BeamModel(name="Sales", title="t", request="r")


def test_source_ref_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        SourceRef(table="raw.orders")
    with pytest.raises(ValidationError):
        SourceRef(table="raw.orders", column="a", expr="b")


def test_duplicate_measure_rejected(model: BeamModel) -> None:
    data = model.model_dump()
    data["events"][0]["measures"].append(data["events"][0]["measures"][0])
    with pytest.raises(ValidationError, match="measure が重複"):
        BeamModel.model_validate(data)


def test_hierarchy_must_reference_attributes(model: BeamModel) -> None:
    data = model.model_dump()
    data["events"][0]["dimensions"][0]["hierarchy"] = ["nope"]
    with pytest.raises(ValidationError, match="階層"):
        BeamModel.model_validate(data)


def test_draft_is_allowed_but_not_finalizable() -> None:
    draft = BeamModel(name="draft", title="下書き", request="売上を見たい")
    issues = draft.finalize_issues()
    assert "ビジネスイベントがありません" in issues
    with pytest.raises(ValueError, match="確定できません"):
        draft.finalize()


def test_missing_grain_and_unknown_availability(model: BeamModel) -> None:
    event = model.events[0]
    event.grain = None
    event.dimensions[1].attributes[0].availability = Availability.UNKNOWN
    issues = model.finalize_issues()
    assert any("粒度" in i for i in issues)
    assert any("product.category がソースで作れるか未確認" in i for i in issues)


def test_non_additive_sum_is_flagged(model: BeamModel) -> None:
    from adp.beam import Aggregation

    model.events[0].measures[2].aggregation = Aggregation.SUM
    assert any("unit_price は足し上げできない" in i for i in model.finalize_issues())


def test_unavailable_requires_gap(model: BeamModel) -> None:
    model.gaps = [g for g in model.gaps if g.related != "customer.annual_income"]
    assert any("annual_income はデータなし" in i for i in model.finalize_issues())


def test_unanswered_gap_blocks(model: BeamModel) -> None:
    model.gaps.append(Gap(kind=GapKind.UNANSWERED, description="返品を売上から引くか"))
    assert "未解決: 返品を売上から引くか" in model.finalize_issues()


def test_metric_references(model: BeamModel) -> None:
    model.metrics.append(
        MetricRequest(
            name="bad",
            label="x",
            question="q",
            framed_question="q",
            event="customer_orders_product",
            measure="gross_margin",
            group_by=["customer.annual_income", "store.nope"],
        )
    )
    model.metrics.append(
        MetricRequest(
            name="ratio",
            label="x",
            question="q",
            framed_question="q",
            type=MetricType.RATIO,
            event="customer_orders_product",
            numerator="revenue",
            denominator="missing",
        )
    )
    issues = model.finalize_issues()
    assert any("gross_margin はデータなし" in i for i in issues)
    assert any("customer.annual_income はデータなし" in i for i in issues)
    assert any("store.nope" in i for i in issues)
    assert any("missing が他の metric に存在しません" in i for i in issues)


def test_conformance_and_matrix(model: BeamModel) -> None:
    second = model.events[0].model_copy(deep=True)
    second.name = "customer_returns_product"
    second.label = "顧客が商品を返品する"
    second.dimensions = [d for d in second.dimensions if d.name in ("customer", "product", "date")]
    model.events.append(second)

    matrix = build_event_matrix(model)
    assert [c.name for c in matrix.columns][:3] == ["customer", "product", "date"]
    conformed = {c.name: c.conformed for c in matrix.columns}
    assert conformed == {
        "customer": True,
        "product": True,
        "date": True,
        "store": False,
        "promotion": False,
        "order_status": False,
        "payment_method": False,
    }
    assert matrix.rows[1].dimensions == {
        "customer": ["customer"],
        "product": ["product"],
        "date": ["date"],
    }

    from adp.beam import W

    second.dimensions[0].w = W.WHAT
    assert any("customer が who と what" in i for i in model.finalize_issues())


def test_json_schema_exports() -> None:
    schema = json_schema()
    defs = schema["$defs"]
    assert {"BeamModel", "EventMatrix", "BusinessEvent"} <= set(defs)
    json.dumps(schema, ensure_ascii=False)


def test_example_keys_must_resolve(model: BeamModel) -> None:
    model.events[0].examples[0].values["store.unknown"] = "x"
    assert any("store.unknown" in i for i in model.finalize_issues())


def test_direct_edit_breaking_structure_is_caught(model: BeamModel) -> None:
    # リストの直接編集は検証されないが、確定時の再検証で検出する
    event = model.events[0]
    event.measures.append(event.measures[0].model_copy())
    issues = model.finalize_issues()
    assert issues and all(i.startswith("構造エラー") for i in issues)
    with pytest.raises(ValueError):
        model.finalize()


def test_time_group_by(model: BeamModel) -> None:
    metric = model.metrics[0]
    for ok in (["date"], ["date.month"], ["order_id"]):
        metric.group_by = ok
        assert model.finalize_issues() == [], ok
    metric.group_by = ["date.hour"]
    assert any("date.hour" in i for i in model.finalize_issues())


def test_role_playing_needs_distinct_fact_keys(model: BeamModel) -> None:
    event = model.events[0]
    customer = event.dimensions[0]
    ship_to = customer.model_copy(update={"role": "ship_to", "label": "配送先顧客"})
    event.dimensions.append(ship_to)
    model.gaps.append(
        Gap(kind=GapKind.DATA_UNAVAILABLE, description="x", related="ship_to.annual_income")
    )
    assert any("fact_key が同じ" in i for i in model.finalize_issues())
    ship_to.fact_key = SourceRef(table="raw.shipments", column="customer_id")
    assert model.finalize_issues() == []


def test_grain_keys_must_resolve(model: BeamModel) -> None:
    model.events[0].grain_keys = ["typo_col"]
    assert any("typo_col" in i for i in model.finalize_issues())


def test_multiple_when_requires_agg_time_dimension(model: BeamModel) -> None:
    event = model.events[0]
    date = event.dimensions[2]
    event.dimensions.append(
        date.model_copy(
            update={
                "role": "paid_on",
                "label": "支払日",
                "fact_key": SourceRef(table="raw.payments", column="paid_at"),
            }
        )
    )
    assert any("agg_time_dimension" in i for i in model.finalize_issues())
    event.agg_time_dimension = "date"
    assert model.finalize_issues() == []
    assert event.time_dimension is date


def test_additivity_rules(model: BeamModel) -> None:
    from adp.beam import Additivity, Aggregation

    m = model.events[0].measures[0]
    m.aggregation = Aggregation.COUNT_DISTINCT
    assert any("count_distinct なので additive" in i for i in model.finalize_issues())
    m.aggregation = Aggregation.SUM
    m.additivity = Additivity.SEMI_ADDITIVE
    assert any("non_additive_dimension" in i for i in model.finalize_issues())
    m.non_additive_dimension = "date"
    assert model.finalize_issues() == []


def test_derived_and_ratio_rules(model: BeamModel) -> None:
    base = {
        "label": "x",
        "question": "q",
        "framed_question": "q",
        "event": "customer_orders_product",
    }
    model.metrics.append(
        MetricRequest.model_validate(
            {**base, "name": "r", "type": "ratio", "numerator": "r", "denominator": "revenue"}
        )
    )
    model.metrics.append(
        MetricRequest.model_validate(
            {**base, "name": "d", "type": "derived", "expr": "revenue; drop table x"}
        )
    )
    issues = model.finalize_issues()
    assert any("[metric r] ratio" in i for i in issues)
    assert any("[metric d] derived" in i for i in issues)


def test_gap_is_scoped_to_event(model: BeamModel) -> None:
    other = model.events[0].model_copy(deep=True, update={"name": "other_event"})
    model.events.append(other)
    issues = model.finalize_issues()
    assert any("[other_event] measure gross_margin はデータなし" in i for i in issues)


def test_deferred_gap_does_not_block(model: BeamModel) -> None:
    model.gaps.append(Gap(kind=GapKind.DEFERRED, description="返品を引くかは後で決める"))
    assert model.finalize_issues() == []


def test_conformed_business_key_must_match(model: BeamModel) -> None:
    other = model.events[0].model_copy(deep=True, update={"name": "other_event"})
    other.dimensions[0].business_key = SourceRef(table="raw.customers", column="customer_name")
    model.events.append(other)
    model.gaps += [g.model_copy(update={"event": "other_event"}) for g in model.gaps]
    assert any("自然キーが" in i for i in model.finalize_issues())


def test_time_grain_only_on_when() -> None:
    from adp.beam import Dimension, W

    with pytest.raises(ValidationError, match="time_grain"):
        Dimension.model_validate(
            {"w": W.WHO, "name": "customer", "label": "顧客", "time_grain": "day"}
        )
