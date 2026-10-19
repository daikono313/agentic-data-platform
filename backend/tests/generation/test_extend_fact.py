"""既にある fct を再利用する要求（S2、#46）: 足りない measure の列と指標だけを足す。

S1（店舗別の売上）で fct_order_lines を作った後に、返品率（注文と返品の 2 イベント）を生成する。
返品率の分母の「販売数量」は fct_order_lines に無い列なので、既存の列・指標を変えずに足す。
"""

import json
from pathlib import Path

import pytest
import yaml

from adp.beam import BeamModel, load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.project import ProjectInfo
from adp.generation.semantic import MetricQuery
from tests.generation.test_dbt_codegen import FIXTURES, _dbt, _warehouse_copy, mf_query

# 生成のテストは固定の記録（#164 で取った 20261016-224137）を使う（eval の記録は取り直すと中身が
# 変わる）。tests/test_e2e_v2.py と同じもの
RETURN_RATE = Path(__file__).parents[1] / "fixtures" / "recorded" / "return_rate_by_category.json"
EVAL_RECORD = Path(__file__).parents[1] / "fixtures" / "evals" / "return_rate_by_category.json"


def _return_rate() -> BeamModel:
    """記録した返品率の成果物。販売数量の measure を既存の列（quantity）と別の名前にする
    （同じ式の既存の列を使い、列を足さないことを確かめる。#113）。"""
    data = json.loads(RETURN_RATE.read_text())["beam"]
    event = next(e for e in data["events"] if e["name"] == "customer_orders_product")
    for m in event["measures"]:
        if m["name"] == "quantity":
            m["name"] = "quantity_sold"
    for x in event["examples"]:
        x["values"] = {
            ("quantity_sold" if k == "quantity" else k): v for k, v in x["values"].items()
        }
    for metric in data["metrics"]:
        if metric["event"] == event["name"] and metric["measure"] == "quantity":
            metric["measure"] = "quantity_sold"
    return BeamModel.model_validate(data)


def _generate(wh: Path, beam: BeamModel):  # type: ignore[no-untyped-def]
    catalog = Catalog(wh / "data" / "dev.duckdb")
    info = ProjectInfo(wh, catalog)
    generation = generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam)
    return info, generation


@pytest.fixture
def after_s1(tmp_path: Path) -> Path:
    wh = _warehouse_copy(tmp_path)
    info, generation = _generate(wh, load(FIXTURES / "beam" / "sales_by_store.yml"))
    info.write(generation.files)
    return wh


def test_existing_fact_gets_missing_measure_and_metrics(after_s1: Path) -> None:
    wh = after_s1
    sql_before = (wh / "models/marts/fct_order_lines.sql").read_text(encoding="utf-8")
    beam = _return_rate()
    info, generation = _generate(wh, beam)
    assert [t.kind for t in generation.tasks if t.kind == "fact_exists"] == []
    updated = {f.path for f in generation.files if f.replaces is not None}
    # 販売数量（quantity_sold = t0.quantity）は、同じ式の既存の列 quantity を使う（列を足さない。
    # #113）。SQL は変えず、YAML に指標だけを足す
    assert updated == {"models/marts/fct_order_lines.yml"}
    info.write(generation.files)

    sql_after = (wh / "models/marts/fct_order_lines.sql").read_text(encoding="utf-8")
    assert sql_after == sql_before and "quantity_sold" not in sql_after
    yml = yaml.safe_load((wh / "models/marts/fct_order_lines.yml").read_text(encoding="utf-8"))
    model = yml["models"][0]
    assert "quantity_sold" not in {c["name"] for c in model["columns"]}  # 列は足していない
    exprs = {m["name"]: m.get("expr") for m in model["metrics"]}
    assert "quantity" in exprs.values() and "quantity_sold" not in exprs.values()
    assert _dbt(wh, "build").returncode == 0
    for q in generation.queries:
        result = mf_query(wh, q)
        assert result.returncode == 0, (q, result.stderr[-500:])
    # S1 の指標もそのまま取れる
    revenue = mf_query(wh, MetricQuery("revenue", ["store__store_name"]))
    assert revenue.returncode == 0, revenue.stderr[-500:]


def test_cross_event_ratio_by_month_can_be_queried(after_s1: Path) -> None:
    """出来事ごとに時間軸の名前が違っても（返品日・注文日）、返品率を月別に問い合わせられる。
    元の指標ごとにその出来事の時間軸（metric_time）で解決される（#185）。"""
    wh = after_s1
    beam = _return_rate()
    rate = next(m for m in beam.metrics if m.name == "return_rate_by_category")
    rate.group_by = ["product.category", "returned_at.month"]
    assert beam.finalize_issues() == []
    info, generation = _generate(wh, beam)
    info.write(generation.files)
    assert _dbt(wh, "build").returncode == 0
    # 分子は返品日、分母は注文日で集計することを、指標の説明に残す（#187）
    note = beam.time_axis_note(rate)
    assert note and any(note in f.content for f in generation.files)
    [q] = [q for q in generation.queries if q.metric == rate.name]
    assert q.group_by == ["product__category", "metric_time__month"]
    result = mf_query(wh, q)
    assert result.returncode == 0, result.stderr[-500:]


def test_hand_written_fact_is_left_to_the_agent(after_s1: Path) -> None:
    wh = after_s1
    path = wh / "models/marts/fct_order_lines.sql"
    path.write_text(path.read_text(encoding="utf-8").replace("-- adp により生成\n", ""))
    beam = _return_rate()
    _, generation = _generate(wh, beam)
    [task] = [t for t in generation.tasks if t.kind == "fact_exists"]
    assert "既存の列と指標は変更・削除しない" in task.instruction
    assert "quantity_sold = t0.quantity" in task.instruction
    assert not any(f.replaces for f in generation.files)


def test_ratio_filter_is_resolved_per_input_event(tmp_path: Path) -> None:
    """比率の指標の絞り込みは、分子（返品）と分母（注文）のそれぞれの出来事の参照名で書く（#178）。"""
    wh = _warehouse_copy(tmp_path)
    beam = BeamModel.model_validate(json.loads(EVAL_RECORD.read_text())["beam"])
    _, generation = _generate(wh, beam)
    ratio = next(
        m
        for f in generation.files
        if f.path.endswith(".yml")
        for m in (yaml.safe_load(f.content) or {}).get("metrics") or []
        if m.get("type") == "ratio"
    )
    assert "filter" not in ratio
    num, den = ratio["numerator"], ratio["denominator"]
    assert num["filter"] != den["filter"]
    assert "Dimension('return__order_status')" in num["filter"]
    assert "Dimension('order_line__order_status')" in den["filter"]


def test_current_eval_record_can_be_queried(after_s1: Path) -> None:
    """今の eval の記録（比率の指標そのものにキャンセル除外がある）から生成した指標を
    問い合わせられる（返品と注文で出来事が分かれていても、絞り込みを元の指標ごとに解決する。#178）。"""
    wh = after_s1
    beam = BeamModel.model_validate(json.loads(EVAL_RECORD.read_text())["beam"])
    info, generation = _generate(wh, beam)
    info.write(generation.files)
    assert _dbt(wh, "build").returncode == 0
    for q in generation.queries:
        assert mf_query(wh, q).returncode == 0, q
