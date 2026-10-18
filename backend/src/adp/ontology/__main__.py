"""`python -m adp.ontology build|check`（#67）。

- build: `ontology/ontology.yml` から `docs/ontology.md` を生成する
- check: md が最新か、源泉・dbt のモデル・BEAM 成果物（beam/）の言葉と食い違いが無いかを調べる
  （CI）。未整理の源泉・dim に列の無い属性は表示だけ
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from adp.catalog.catalog import Catalog, CatalogUnavailable
from adp.ontology.check import beam_mismatches, check
from adp.ontology.models import dump, load
from adp.ontology.render import markdown
from adp.warehouse import warehouse_db

REPO_ROOT = Path(__file__).resolve().parents[4]
ONTOLOGY = REPO_ROOT / "ontology" / "ontology.yml"
DOC = REPO_ROOT / "docs" / "ontology.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m adp.ontology")
    parser.add_argument("command", choices=["build", "check"])
    args = parser.parse_args(argv)
    ontology = load(ONTOLOGY)
    if args.command == "build":
        # 正本も決まった形に整える（手で編集した後でも CI の検査と一致させる）
        ONTOLOGY.write_text(dump(ontology), encoding="utf-8")
        DOC.write_text(markdown(ontology), encoding="utf-8")
        print(f"{DOC.relative_to(REPO_ROOT)} を生成しました（ontology.yml も整形しました）")
        return 0

    failed = False
    if not DOC.exists() or DOC.read_text(encoding="utf-8") != markdown(ontology):
        print("docs/ontology.md が ontology.yml と一致しません（make ontology で生成し直す）")
        failed = True
    warehouse = REPO_ROOT / "warehouse"
    db = warehouse_db(warehouse)
    manifest = warehouse / "target" / "manifest.json"
    if not db.exists() or not manifest.exists():
        print("源泉・dbt との照合には DB と manifest が要る（make dbt-build を先に実行する）")
        return 1
    try:
        result = check(
            ontology,
            Catalog(db),
            manifest,
        )
    except CatalogUnavailable as err:
        print(f"源泉と照合できなかった: {err}")
        return 1
    errors = result.errors + beam_mismatches(ontology, REPO_ROOT / "beam")
    for e in errors:
        print(f"✗ {e}")
    for s in result.unmapped_sources:
        print(f"・未整理の源泉（どの概念にもなっていない）: {s}")
    for m in result.unmapped_models:
        print(f"・概念を持たない共通のモデル: {m}")
    for a in result.unmodeled_attributes:
        print(f"・dim に列の無い属性（要求で使われたら生成が足す）: {a}")
    if errors:
        failed = True
    if not failed:
        print(f"✓ オントロジー: 概念 {len(ontology.concepts)}、関係 {len(ontology.relations)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
