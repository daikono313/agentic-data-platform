"""生成した dbt モデルを、リポジトリの warehouse を複製した一時プロジェクトで実際に build する。"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.project import ProjectInfo

ROOT = Path(__file__).parents[3]
FIXTURES = Path(__file__).parents[1] / "fixtures"
DBT = Path(sys.executable).parent / "dbt"


def _warehouse_copy(tmp: Path) -> Path:
    dst = tmp / "warehouse"
    shutil.copytree(
        ROOT / "warehouse",
        dst,
        ignore=shutil.ignore_patterns("target", "data", "logs", "dbt_packages", ".user.yml"),
    )
    spec = importlib.util.spec_from_file_location(
        "gen_sample", dst / "scripts/generate_sample_data.py"
    )
    assert spec and spec.loader
    gen = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gen
    spec.loader.exec_module(gen)
    gen.load(dst / "data" / "dev.duckdb", gen.generate(500, 3))
    return dst


def _dbt(warehouse: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"DBT_PROFILES_DIR": str(warehouse)}
    return subprocess.run(
        [str(DBT), *args], cwd=warehouse, env=env, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _warehouse_copy(tmp_path_factory.mktemp("gen"))


def test_generated_models_build_and_match_reference(warehouse: Path) -> None:
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    assert design.blockers == []
    generation = generate_models(design, info)

    # 共通ディメンションと staging は既存のものを再利用し、fct だけを生成する
    assert [f.path for f in generation.files] == [
        "models/marts/fct_order_lines.sql",
        "models/marts/fct_order_lines.yml",
    ]
    assert {"dim_customer", "dim_product", "dim_store", "dim_promotion", "stg_orders"} <= set(
        generation.reused
    )
    assert generation.tasks == []
    info.write(generation.files)
    with pytest.raises(FileExistsError):
        info.write(generation.files)

    # 手書きのリファレンスも同じプロジェクトに入れて比較する
    ref_sql = (FIXTURES / "generation/reference/fct_order_lines.sql").read_text(encoding="utf-8")
    (warehouse / "models/marts/ref_fct_order_lines.sql").write_text(ref_sql, encoding="utf-8")

    _dbt(warehouse, "deps")
    result = _dbt(warehouse, "build")
    assert result.returncode == 0, result.stdout[-3000:]

    con = duckdb.connect(str(warehouse / "data" / "dev.duckdb"), read_only=True)
    columns = (
        "order_line_id, order_id, customer_key, product_key, store_key, promotion_key,"
        " ordered_on, ordered_at, payment_method, quantity, unit_price, sales_amount"
    )
    for a, b in (
        ("fct_order_lines", "ref_fct_order_lines"),
        ("ref_fct_order_lines", "fct_order_lines"),
    ):
        diff = con.sql(f"select {columns} from main.{a} except select {columns} from main.{b}")
        assert diff.fetchall() == [], f"{a} にだけある行があります"


def test_new_dimension_and_staging_are_generated(tmp_path: Path) -> None:
    """既存モデルが無い場合は staging と dim も生成し、build が通る。"""
    warehouse = _warehouse_copy(tmp_path)
    for name in ("dim_store", "stg_stores"):
        for p in (warehouse / "models").rglob(f"{name}.sql"):
            p.unlink()
    for yml in (warehouse / "models").rglob("_*.yml"):
        text = yml.read_text(encoding="utf-8")
        for name in ("dim_store", "stg_stores"):
            text = _drop_model_entry(text, name)
        yml.write_text(text, encoding="utf-8")

    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    design = DesignBuilder(CatalogSchema(catalog)).build(
        load(FIXTURES / "beam" / "sales_by_store.yml")
    )
    generation = generate_models(design, info)
    paths = [f.path for f in generation.files]
    assert "models/staging/stg_stores.sql" in paths and "models/marts/dim_store.sql" in paths
    dim_sql = next(f.content for f in generation.files if f.path.endswith("dim_store.sql"))
    assert "'不明'" in dim_sql and "{{ unknown_key() }}" in dim_sql
    info.write(generation.files)
    _dbt(warehouse, "deps")
    result = _dbt(warehouse, "build")
    assert result.returncode == 0, result.stdout[-3000:]


def _drop_model_entry(text: str, name: str) -> str:
    import yaml

    data = yaml.safe_load(text)
    for key in ("models",):
        if key in data:
            data[key] = [m for m in data[key] if m.get("name") != name]
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
