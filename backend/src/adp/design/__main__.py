"""BEAM 成果物から物理設計 JSON と ER 図を出力する。

uv run python -m adp.design ../beam/sales_by_store.yml [--er]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.design.builder import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.design.er import er_diagram
from adp.warehouse import warehouse_db

REPO_ROOT = Path(__file__).resolve().parents[4]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("beam", type=Path)
    parser.add_argument("--db", type=Path, default=warehouse_db(REPO_ROOT / "warehouse"))
    parser.add_argument("--er", action="store_true", help="ER 図（mermaid）を出力する")
    args = parser.parse_args()
    design = DesignBuilder(CatalogSchema(Catalog(args.db))).build(load(args.beam))
    print(er_diagram(design) if args.er else design.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
