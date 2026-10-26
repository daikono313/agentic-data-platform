"""按分した fct（ADR 0010）を実際の dbt で作り、二重計上しないことを確かめる。"""

import csv
import os
import subprocess
import sys
from pathlib import Path

import duckdb

from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.evals.repair import prepare_warehouse
from adp.generation import generate_models
from adp.generation.conventions import check_warehouse
from adp.generation.project import ProjectInfo

FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "promotion_sales.yml"
BIN = Path(sys.executable).parent


def test_allocated_fact_sums_to_the_total(tmp_path: Path) -> None:
    warehouse = prepare_warehouse(tmp_path, orders=500)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    beam = load(FIXTURE)
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    assert design.blockers == []
    info = ProjectInfo(warehouse, catalog)
    generation = generate_models(design, info, beam)
    info.write(generation.files)
    # 既存の dim_promotion への役割の entity はエージェントへの作業（add_role_entities）なので、
    # ここでは作業の結果を手で足す
    assert [t.kind for t in generation.tasks] == ["add_role_entities"]
    marts = warehouse / "models" / "marts" / "_marts.yml"
    head = "  - name: dim_promotion\n"
    text = marts.read_text(encoding="utf-8")
    at = text.index("      enabled: true\n", text.index(head)) + len("      enabled: true\n")
    role = (
        "    derived_semantics:\n      entities:\n"
        "        - {type: unique, name: applied_promotion, expr: promotion_key}\n"
    )
    marts.write_text(text[:at] + role + text[at:], encoding="utf-8")

    env = os.environ | {"DBT_PROFILES_DIR": str(warehouse)}

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args), cwd=warehouse, env=env, capture_output=True, text=True, check=False
        )

    for args in (["deps"], ["build"]):
        result = run(str(BIN / "dbt"), *args)
        assert result.returncode == 0, result.stdout[-3000:]
    assert [str(v) for v in check_warehouse(warehouse)] == []

    out = tmp_path / "q.csv"
    result = run(
        str(BIN / "mf"),
        "query",
        "--metrics",
        "promotion_sales",
        "--group-by",
        "applied_promotion__promotion_name",
        "--csv",
        str(out),
    )
    assert result.returncode == 0, result.stdout[-3000:]
    allocated = sum(float(r["promotion_sales"]) for r in csv.DictReader(out.open()))
    con = duckdb.connect(str(warehouse / "data" / "dev.duckdb"), read_only=True)
    (total,) = con.execute(
        "select sum(quantity * unit_price - discount_amount) from main.stg_order_items"
        " join main.stg_orders using (order_id) where order_status <> 'cancelled'"
    ).fetchone()  # type: ignore[misc]
    (multi,) = con.execute(
        "select count(*) from (select order_id from main.stg_order_promotions"
        " group by order_id having count(*) > 1)"
    ).fetchone()  # type: ignore[misc]
    con.close()
    assert multi > 0  # 2 つのプロモーションが付く注文がある（按分が効く）
    assert abs(allocated - float(total)) < 1e-6 * max(1.0, abs(float(total)))
