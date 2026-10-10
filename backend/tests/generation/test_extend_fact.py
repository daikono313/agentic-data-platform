"""既にある fct を再利用する要求（S2、#46）: 足りない measure の列と指標だけを足す。

S1（店舗別の売上）で fct_order_lines を作った後に、返品率（注文と返品の 2 イベント）を生成する。
返品率の分母の「販売数量」は fct_order_lines に無い列なので、既存の列・指標を変えずに足す。
"""

import json
from pathlib import Path

import pytest

from adp.beam import BeamModel, load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.project import ProjectInfo
from adp.generation.semantic import MetricQuery, run_query
from tests.generation.test_dbt_codegen import DBT, FIXTURES, _dbt, _warehouse_copy

RETURN_RATE = Path(__file__).parents[1] / "fixtures" / "evals" / "return_rate_by_category.json"


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
    beam = BeamModel.model_validate(json.loads(RETURN_RATE.read_text())["beam"])
    info, generation = _generate(wh, beam)
    assert [t.kind for t in generation.tasks if t.kind == "fact_exists"] == []
    updated = {f.path for f in generation.files if f.replaces is not None}
    assert updated == {"models/marts/fct_order_lines.sql", "models/marts/fct_order_lines.yml"}
    info.write(generation.files)

    sql_after = (wh / "models/marts/fct_order_lines.sql").read_text(encoding="utf-8")
    # 既存の列はそのまま、足りない列だけを足す
    assert sql_after.startswith(sql_before.rstrip("\n").rsplit("\nfrom ", 1)[0].rstrip())
    assert "as quantity_sold" in sql_after
    assert _dbt(wh, "build").returncode == 0
    for q in generation.queries:
        result = run_query(wh, DBT.parent / "mf", q)
        assert result.returncode == 0, (q, result.stderr[-500:])
    # S1 の指標もそのまま取れる
    revenue = run_query(wh, DBT.parent / "mf", MetricQuery("revenue", ["store__store_name"]))
    assert revenue.returncode == 0, revenue.stderr[-500:]


def test_hand_written_fact_is_left_to_the_agent(after_s1: Path) -> None:
    wh = after_s1
    path = wh / "models/marts/fct_order_lines.sql"
    path.write_text(path.read_text(encoding="utf-8").replace("-- adp により生成\n", ""))
    beam = BeamModel.model_validate(json.loads(RETURN_RATE.read_text())["beam"])
    _, generation = _generate(wh, beam)
    [task] = [t for t in generation.tasks if t.kind == "fact_exists"]
    assert "既存の列と指標は変更・削除しない" in task.instruction
    assert "quantity_sold = t0.quantity" in task.instruction
    assert not any(f.replaces for f in generation.files)
