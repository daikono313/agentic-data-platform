"""`python -m adp.ontology build|check`（#67）。

- build: `ontology/ontology.yml` から `docs/ontology.md` を生成する
- check: md が最新か、源泉・dbt のモデルと食い違いが無いかを調べる（CI）。未整理の源泉は表示だけ
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from adp.catalog.catalog import Catalog
from adp.ontology.check import check
from adp.ontology.models import load
from adp.ontology.render import markdown

REPO_ROOT = Path(__file__).resolve().parents[4]
ONTOLOGY = REPO_ROOT / "ontology" / "ontology.yml"
DOC = REPO_ROOT / "docs" / "ontology.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m adp.ontology")
    parser.add_argument("command", choices=["build", "check"])
    args = parser.parse_args(argv)
    ontology = load(ONTOLOGY)
    if args.command == "build":
        DOC.write_text(markdown(ontology), encoding="utf-8")
        print(f"{DOC.relative_to(REPO_ROOT)} を生成しました")
        return 0

    failed = False
    if not DOC.exists() or DOC.read_text(encoding="utf-8") != markdown(ontology):
        print("docs/ontology.md が ontology.yml と一致しません（make ontology で生成し直す）")
        failed = True
    warehouse = REPO_ROOT / "warehouse"
    result = check(
        ontology,
        Catalog(warehouse / "data" / "dev.duckdb"),
        warehouse / "target" / "manifest.json",
    )
    for e in result.errors:
        print(f"✗ {e}")
    for s in result.unmapped_sources:
        print(f"・未整理の源泉（どの概念にもなっていない）: {s}")
    if result.errors:
        failed = True
    if not failed:
        print(f"✓ オントロジー: 概念 {len(ontology.concepts)}、関係 {len(ontology.relations)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
