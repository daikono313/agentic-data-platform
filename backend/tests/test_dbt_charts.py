"""dbt Charts の PoC（#40）: この warehouse（DuckDB + dbt プロファイル）で描画でき、
DuckDB の外部アクセス禁止が dbt Charts からの接続にも効くこと。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.project import ProjectInfo
from tests.generation.test_dbt_codegen import DBT, FIXTURES, _dbt, _warehouse_copy

DCT = Path(sys.executable).parent / "dct"

BOARD = """\
title: 店舗別の売上
source: warehouse
queries:
  by_store:
    sql: |
      select s.store_name, sum(f.sales_amount) as sales
      from fct_order_lines f join dim_store s using (store_key)
      group by 1 order by 2 desc limit 5
charts:
  stores:
    type: bar
    query: by_store
    x: store_name
    y: sales
rows:
  - stores
"""


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("charts")
    wh = _warehouse_copy(root)
    catalog = Catalog(wh / "data" / "dev.duckdb")
    info = ProjectInfo(wh, catalog)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    info.write(generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam).files)
    assert _dbt(wh, "build").returncode == 0
    (wh / "charts" / "store_sales.yml").write_text(BOARD, encoding="utf-8")
    return wh


def _dct(wh: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"DBT_PROFILES_DIR": str(wh), "DCT_NO_WORKSPACE_GUARD": "1", "NO_COLOR": "1"}
    return subprocess.run(
        [str(DCT), *args], cwd=wh, env=env, capture_output=True, text=True, check=False, timeout=300
    )


def test_board_renders_from_dbt_profile(warehouse: Path) -> None:
    text = _dct(warehouse, "render", "charts/store_sales.yml", "--format", "text")
    assert text.returncode == 0, text.stdout + text.stderr
    assert "5 rows" in text.stdout and "store_name" in text.stdout
    svg = warehouse / "store_sales.svg"
    out = _dct(
        warehouse, "render", "charts/store_sales.yml", "--format", "svg", "--output", str(svg)
    )
    assert out.returncode == 0 and svg.read_text().lstrip().startswith("<")


def test_external_access_is_blocked_through_dct(warehouse: Path, tmp_path: Path) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("secret-outside")
    result = _dct(warehouse, "query", "warehouse", f"select * from read_text('{secret}')")
    output = result.stdout + result.stderr
    assert result.returncode != 0
    # 外部アクセス禁止で拒否されたこと（プロファイルが見つからない等の別の失敗ではないこと）
    assert "disabled by configuration" in " ".join(output.split())
    assert "secret-outside" not in output


def test_dbt_charts_config_is_protected_from_agent(tmp_path: Path) -> None:
    from adp.generation.guard import WorkspaceGuard

    guard = WorkspaceGuard(tmp_path)
    assert guard("Edit", {"file_path": "dbt_charts.yml", "old_string": "a", "new_string": "b"})
    assert guard("Write", {"file_path": "dbt_charts.yml", "content": "sources: {}"})


def test_dct_is_installed() -> None:
    assert shutil.which(str(DCT)) or DCT.exists()
    assert DBT.exists()
