"""`python -m adp.cleansing build|check|draft`（#68）。

- build: `warehouse/cleansing/*.yml` から `warehouse/models/staging/stg_*.sql|yml` を生成する
- check: 定義と源泉の矛盾、生成物が最新か、定義の無い生成物が残っていないか（CI）。
  型変換の失敗は警告
- draft <schema.table>: 源泉のプロファイルから定義の雛形を標準出力に出す
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from adp.catalog.catalog import Catalog
from adp.cleansing.check import check_all, draft
from adp.cleansing.models import dump_spec
from adp.warehouse import warehouse_db

REPO_ROOT = Path(__file__).resolve().parents[4]
WAREHOUSE = REPO_ROOT / "warehouse"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m adp.cleansing")
    parser.add_argument("command", choices=["build", "check", "draft"])
    parser.add_argument("source", nargs="?")
    args = parser.parse_args(argv)
    catalog = Catalog(warehouse_db(WAREHOUSE), WAREHOUSE / "target" / "manifest.json")

    if args.command == "draft":
        if not args.source:
            parser.error("draft には源泉（例: raw.orders）を指定する")
        print(dump_spec(draft(catalog.profile_table(args.source))), end="")
        return 0

    result = check_all(WAREHOUSE, catalog)
    for w in result.warnings:
        print(f"⚠ {w}")
    for e in result.errors:
        print(f"✗ {e}")
    if args.command == "build":
        for r in result.rendered:
            for rel, text in ((r.sql_path, r.sql), (r.yml_path, r.yml)):
                (WAREHOUSE / rel).write_text(text, encoding="utf-8")
        print(f"クレンジング層を {len(result.rendered)} モデル生成しました")
        return 1 if result.errors else 0

    stale = [
        rel
        for r in result.rendered
        for rel, text in ((r.sql_path, r.sql), (r.yml_path, r.yml))
        if not (WAREHOUSE / rel).exists() or (WAREHOUSE / rel).read_text(encoding="utf-8") != text
    ]
    for rel in stale:
        print(f"✗ {rel} が定義と一致しない（make cleansing で生成し直す）")
    if result.errors or stale:
        return 1
    print(f"✓ クレンジング: {len(result.rendered)} モデル")
    return 0


if __name__ == "__main__":
    sys.exit(main())
