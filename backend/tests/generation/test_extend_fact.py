"""既にある fct を再利用する要求（S2、#46）: 足りない measure の列と指標だけを足す。

S1（店舗別の売上）で fct_order_lines を作った後に、返品率（注文と返品の 2 イベント）を生成する。
返品率の分母の「販売数量」は fct_order_lines に無い列なので、既存の列・指標を変えずに足す。
"""

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

# eval の記録（#164 で取り直した 20261016-224137）から取った返品率の成果物。
# eval の記録は取り直すたびに中身が変わるので、生成のテストはこの固定の成果物を使う
RETURN_RATE = Path(__file__).parents[1] / "fixtures" / "beam" / "return_rate_by_category.yml"


def _return_rate() -> BeamModel:
    """記録した返品率の成果物。販売数量の measure を既存の列（quantity）と別の名前にする
    （同じ式の既存の列を使い、列を足さないことを確かめる。#113）。"""
    data = load(RETURN_RATE).model_dump(mode="json")
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
