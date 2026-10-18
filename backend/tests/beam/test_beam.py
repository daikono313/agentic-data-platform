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
    MetricFilter,
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
    from adp.beam import Additivity, Aggregation, StoryType

    m = model.events[0].measures[0]
    m.aggregation = Aggregation.COUNT_DISTINCT
    assert any("count_distinct なので additive" in i for i in model.finalize_issues())
    m.aggregation = Aggregation.SUM
    m.additivity = Additivity.SEMI_ADDITIVE
    assert any("non_additive_dimension" in i for i in model.finalize_issues())
    m.non_additive_dimension = "date"
    assert any("story_type: recurring" in i for i in model.finalize_issues())
    model.events[0].story_type = StoryType.RECURRING
    assert model.finalize_issues() == []


def test_semi_additive_measure_cannot_be_cumulative(model: BeamModel) -> None:
    """残高のような時点の値は、累計すると意味が変わる（#134）。"""
    from adp.beam import Additivity, MetricType

    metric = next(x for x in model.metrics if x.measure)
    m = model.events[0].measure(metric.measure or "")
    assert m is not None
    m.additivity = Additivity.SEMI_ADDITIVE
    m.non_additive_dimension = "date"
    metric.type = MetricType.CUMULATIVE
    metric.window = "7 days"
    assert any("累計にできません" in i for i in model.finalize_issues())


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


def test_nested_inputs_must_resolve_refs() -> None:
    """元の指標がさらに比率で、その下の指標が別の出来事にあるときも、切り口・絞り込みを確かめる
    （#180）。"""
    record = Path(__file__).parents[1] / "fixtures" / "recorded" / "return_rate_by_category.json"
    model = BeamModel.model_validate(json.loads(record.read_text(encoding="utf-8"))["beam"])
    assert model.finalize_issues() == []
    base = {"label": "x", "question": "q", "framed_question": "q"}
    # 返品の出来事の派生の指標が、返品率（返品の出来事。分母は注文の出来事）を使う。
    # 返品理由は注文の出来事に無いので、MetricFlow が分母の指標に渡すと解決できない
    model.metrics.append(
        MetricRequest.model_validate(
            base
            | {
                "name": "rate_pct",
                "event": "customer_returns_product",
                "type": "derived",
                "expr": "return_rate_by_category * 100",
                "filters": [
                    {"ref": "return_reason.return_reason", "operator": "!=", "values": ["other"]}
                ],
            }
        )
    )
    issues = model.finalize_issues()
    assert any(
        "[metric rate_pct] return_reason.return_reason は元の指標 return_rate_by_category の元の "
        "sold_qty_excl_cancelled のイベント customer_orders_product にありません" in i
        for i in issues
    ), issues
    # 商品カテゴリなら、たどった先の指標（返品・注文）のどちらでも解決できる
    model.metrics[-1].filters[0].ref = "product.category"
    assert model.finalize_issues() == []

    # 途中の比率の置き場所（返品の出来事）は問わない。たどった先の注文の指標で解決できればよい
    orders = {"event": "customer_orders_product"}
    model.metrics += [
        MetricRequest.model_validate(
            base
            | {"name": "self_rate", "event": "customer_returns_product", "type": "ratio"}
            | {"numerator": "sold_qty_excl_cancelled", "denominator": "sold_qty_excl_cancelled"}
        ),
        MetricRequest.model_validate(
            base
            | orders
            | {"name": "t", "type": "derived", "expr": "self_rate * 2"}
            | {"filters": [{"ref": "order_status.order_status", "operator": "=", "values": ["x"]}]}
        ),
    ]
    assert model.finalize_issues() == []


def test_cross_event_group_by_must_share_names() -> None:
    """出来事をまたぐ指標の切り口は、出来事によらず同じ名前で解決できるものに限る。
    マスタなし・縮退のディメンションと時間軸以外の When は、出来事ごとの名前になる（#181）。"""
    record = Path(__file__).parents[1] / "fixtures" / "recorded" / "return_rate_by_category.json"
    model = BeamModel.model_validate(json.loads(record.read_text(encoding="utf-8"))["beam"])
    returns = model.event("customer_returns_product")
    orders = model.event("customer_orders_product")
    assert returns is not None and orders is not None
    # 注文の状態（マスタなし）・注文日（返品では時間軸でない）・返品番号（縮退）を両方に持たせる
    shared = ("order_status", "ordered_at")
    returns.dimensions += [d.model_copy(deep=True) for d in orders.dimensions if d.ref in shared]
    returns.agg_time_dimension = "returned_at"
    orders.degenerate_dimensions += [d.model_copy() for d in returns.degenerate_dimensions]
    rate = model.metrics[-1]
    assert rate.name == "return_rate_by_category"
    assert model.finalize_issues() == []

    def unshared() -> list[str]:
        return [i for i in model.finalize_issues() if "別々に記録される" in i]

    for ref in ("order_status.order_status", "ordered_at.month", "return_id"):
        rate.group_by = ["product.category", ref]
        assert unshared() == [
            f"[metric return_rate_by_category] 切り口 {ref} は出来事（customer_returns_product"
            "・customer_orders_product）ごとに別々に記録される項目なので、出来事をまたぐ指標の"
            "切り口に使えません。どの出来事にも共通するディメンション（商品など）の属性で"
            "切ってください"
        ], ref

    # 片方の出来事にしか無い切り口は「ありません」だけを出す
    rate.group_by = ["return_reason.return_reason"]
    assert unshared() == []
    assert any("return_reason.return_reason は元の指標" in i for i in model.finalize_issues())

    # 絞り込みは元の指標ごとに、その出来事の名前で書くので問わない（#178）
    rate.group_by = ["product.category"]
    rate.filters = [
        MetricFilter.model_validate(
            {"ref": "order_status.order_status", "operator": "=", "values": ["x"]}
        )
    ]
    assert unshared() == []

    # 同じ出来事の中だけなら、出来事ごとの名前でも解決できる
    sold = next(m for m in model.metrics if m.name == "sold_qty_excl_cancelled")
    model.metrics.append(
        MetricRequest.model_validate(
            {"label": "x", "question": "q", "framed_question": "q"}
            | {"name": "half", "event": "customer_orders_product", "type": "ratio"}
            | {"numerator": sold.name, "denominator": sold.name}
            | {"group_by": ["order_status.order_status"]}
        )
    )
    assert not any("[metric half]" in i for i in model.finalize_issues())

    # 入れ子でも、たどった先の出来事で判定する
    model.metrics.append(
        MetricRequest.model_validate(
            {"label": "x", "question": "q", "framed_question": "q"}
            | {"name": "pct", "event": "customer_orders_product", "type": "derived"}
            | {"expr": "return_rate_by_category * 100", "group_by": ["return_id"]}
        )
    )
    assert any(i.startswith("[metric pct] 切り口 return_id") for i in unshared())


def test_cross_event_group_by_time_uses_each_time_axis() -> None:
    """時間軸の切り口は元の指標ごとにその出来事の時間軸で解決されるので、出来事ごとに時間軸の
    名前が違っても切れる（#185）。"""
    record = Path(__file__).parents[1] / "fixtures" / "recorded" / "return_rate_by_category.json"
    model = BeamModel.model_validate(json.loads(record.read_text(encoding="utf-8"))["beam"])
    rate = model.metrics[-1]
    assert rate.name == "return_rate_by_category" and rate.event == "customer_returns_product"
    rate.group_by = ["product.category", "returned_at.month"]
    assert model.finalize_issues() == []

    # 元の指標の出来事に時間軸が無ければ切れない
    orders = model.event("customer_orders_product")
    assert orders is not None
    orders.dimensions += [orders.dimensions[1].model_copy(update={"role": "shipped_at"})]
    orders.agg_time_dimension = None
    assert [i for i in model.finalize_issues() if "returned_at.month" in i] == [
        "[metric return_rate_by_category] returned_at.month は時間軸で切りますが、元の指標 "
        "sold_qty_excl_cancelled のイベント customer_orders_product には時間軸"
        "（agg_time_dimension）がありません"
    ]


def test_metric_input_cycle_is_reported(model: BeamModel) -> None:
    base = {"label": "x", "question": "q", "framed_question": "q"}
    base |= {"event": "customer_orders_product"}
    model.metrics += [
        MetricRequest.model_validate(base | {"name": "a", "type": "derived", "expr": "b + 1"}),
        MetricRequest.model_validate(base | {"name": "b", "type": "derived", "expr": "a + 1"}),
    ]
    issues = model.finalize_issues()
    assert any("[metric a] 元の指標をたどると a に戻ります" in i for i in issues), issues
