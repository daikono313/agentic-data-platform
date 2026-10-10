"""dbt Charts から Semantic Layer（MetricFlow）の指標を使う（#41、design-doc O4 の案 (a)）。

チャートの元ファイル `warehouse/charts/metrics/<board>.yml` には、SQL ではなく指標と切り口を書く。

    title: 店舗別の売上
    metric_queries:
      by_store:
        metrics: [sales_amount_total]
        group_by: [store__store_name]
        order: [-sales_amount_total]
        limit: 10
    charts:
      stores: {type: bar, query: by_store, x: store__store_name, y: sales_amount_total}
    rows: [stores]

`compile` は各 metric_query を MetricFlow で SQL にし（`mf query --explain`）、dbt Charts の
ボード `warehouse/charts/<board>.yml` を書き出す。集計の定義は Semantic Layer の 1 か所だけにあり、
チャート側で書き直さない（design-doc R5）。`check` は、ボードがすべてこの仕組みで作られ、
最新であることを確かめる（手書きの SQL を入れない）。

    uv run python -m adp.charts compile   # ボードを書き出す
    uv run python -m adp.charts check     # CI: 古いボード・手書きのボードがあれば失敗
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

HEADER = (
    "# adp により生成（charts/metrics/{name}.yml から `python -m adp.charts compile`）。"
    "編集しない\n"
)
SOURCE = "warehouse"
MF_TIMEOUT_S = 120
_QUERY_KEYS = {"metrics", "group_by", "order", "limit", "where", "start_time", "end_time"}
_URL = re.compile(r"^\s*[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _unsafe(value: Any, path: str = "") -> list[str]:
    """ボードの中の、DuckDB を通らない外部への経路（URL・http クエリ）を探す（ADR 0002）。"""
    found: list[str] = []
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() == "http":
            found.append(f"{path or '/'}: type: http は使えません")
        for k, v in value.items():
            found += _unsafe(v, f"{path}/{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += _unsafe(v, f"{path}/{i}")
    elif isinstance(value, str) and _URL.match(value):
        found.append(f"{path}: URL は使えません（{value[:40]}）")
    return found


class ChartError(Exception):
    pass


@dataclass
class MetricQuery:
    metrics: list[str]
    group_by: list[str]
    order: list[str]
    limit: int | None
    where: str | None
    start_time: str | None
    end_time: str | None

    @classmethod
    def parse(cls, name: str, data: dict[str, Any]) -> MetricQuery:
        unknown = set(data) - _QUERY_KEYS
        if unknown:
            raise ChartError(f"metric_queries.{name} に使えないキーがあります: {sorted(unknown)}")
        if not data.get("metrics"):
            raise ChartError(f"metric_queries.{name} に metrics がありません")
        return cls(
            metrics=list(data["metrics"]),
            group_by=list(data.get("group_by") or []),
            order=list(data.get("order") or []),
            limit=data.get("limit"),
            where=data.get("where"),
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
        )

    def args(self) -> list[str]:
        args = ["--metrics", ",".join(self.metrics)]
        if self.group_by:
            args += ["--group-by", ",".join(self.group_by)]
        if self.order:
            args += ["--order", ",".join(self.order)]
        if self.limit is not None:
            args += ["--limit", str(int(self.limit))]
        if self.where:
            args += ["--where", self.where]
        if self.start_time:
            args += ["--start-time", self.start_time]
        if self.end_time:
            args += ["--end-time", self.end_time]
        return args


def compile_sql(warehouse: Path, mf: Path, query: MetricQuery) -> str:
    """MetricFlow に SQL を作らせる。DB ファイル名に依存しないよう、カタログ名は外す。"""
    result = subprocess.run(
        [str(mf), "query", *query.args(), "--explain"],
        cwd=warehouse,
        env=os.environ | {"DBT_PROFILES_DIR": str(warehouse), "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=MF_TIMEOUT_S,
    )
    lines = result.stdout.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.lstrip().upper().startswith(("SELECT", "WITH"))),
        None,
    )
    if result.returncode != 0 or start is None:
        raise ChartError(f"MetricFlow で SQL を作れませんでした: {' '.join(query.args())}")
    sql = "\n".join(lines[start:]).strip()
    tree = sqlglot.parse_one(sql, read="duckdb")
    for table in tree.find_all(exp.Table):
        table.set("catalog", None)
    return tree.sql(dialect="duckdb", pretty=True)


def compile_board(name: str, spec: dict[str, Any], warehouse: Path, mf: Path) -> str:
    unsafe = _unsafe(spec)
    if unsafe:
        raise ChartError(f"{name}: " + " / ".join(unsafe))
    if "queries" in spec:
        raise ChartError(f"{name}: queries（SQL）は書けません。metric_queries を使ってください")
    if spec.get("source", SOURCE) != SOURCE:
        raise ChartError(f"{name}: source は {SOURCE} だけです")
    queries = spec.get("metric_queries") or {}
    if not queries:
        raise ChartError(f"{name}: metric_queries がありません")
    board = {k: v for k, v in spec.items() if k != "metric_queries"}
    board["source"] = SOURCE
    board["queries"] = {
        q: {"sql": compile_sql(warehouse, mf, MetricQuery.parse(q, data))}
        for q, data in queries.items()
    }
    ordered: dict[str, Any] = {k: board[k] for k in ("title", "source", "queries") if k in board}
    ordered |= {k: v for k, v in board.items() if k not in ordered}
    body = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=100)
    return HEADER.format(name=name) + body


def _specs(warehouse: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted((warehouse / "charts" / "metrics").glob("*.yml"))}


def compile_all(warehouse: Path, mf: Path) -> dict[str, str]:
    """すべての元ファイルからボードを作る（ファイル名 → 内容）。書き込みはしない。"""
    return {
        name: compile_board(name, yaml.safe_load(path.read_text(encoding="utf-8")), warehouse, mf)
        for name, path in _specs(warehouse).items()
    }


def write_all(warehouse: Path, mf: Path) -> list[Path]:
    written = []
    for name, text in compile_all(warehouse, mf).items():
        path = warehouse / "charts" / f"{name}.yml"
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def check(warehouse: Path, mf: Path) -> list[str]:
    """ボードの問題（手書き・古い・元ファイルが無い）を返す。空なら問題なし。"""
    problems = _check_sources(warehouse)
    expected = compile_all(warehouse, mf)
    for path in sorted((warehouse / "charts").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("# adp により生成"):
            problems.append(
                f"{path.name}: 手書きのボードは置けません（charts/metrics/ に元ファイルを書く）"
            )
        elif path.stem not in expected:
            problems.append(f"{path.name}: 元ファイル charts/metrics/{path.stem}.yml がありません")
        elif text != expected[path.stem]:
            problems.append(f"{path.name}: 古くなっています（python -m adp.charts compile を実行）")
    for name in expected:
        if not (warehouse / "charts" / f"{name}.yml").exists():
            problems.append(
                f"{name}.yml: まだ書き出されていません（python -m adp.charts compile を実行）"
            )
    return problems


def _check_sources(warehouse: Path) -> list[str]:
    """dbt Charts の接続は dbt プロファイル経由の warehouse だけにする。

    独自の duckdb ソースは外部アクセスを有効に戻せるため（ADR 0002）。
    """
    path = warehouse / "dbt_charts.yml"
    if not path.exists():
        return ["dbt_charts.yml がありません"]
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = config.get("sources") or {}
    if set(sources) != {SOURCE} or (sources.get(SOURCE) or {}).get("type") != "dbt_profile":
        return [f"dbt_charts.yml: ソースは {SOURCE}（type: dbt_profile）の 1 つだけにしてください"]
    return [f"dbt_charts.yml: {p}" for p in _unsafe(config)]


def main() -> None:
    from adp.llm.settings import REPO_ROOT

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["compile", "check"])
    parser.add_argument("--warehouse", type=Path, default=REPO_ROOT / "warehouse")
    args = parser.parse_args()
    mf = Path(sys.executable).parent / "mf"
    try:
        if args.command == "compile":
            for path in write_all(args.warehouse, mf):
                print(f"wrote {path.relative_to(args.warehouse)}")
            return
        problems = check(args.warehouse, mf)
    except ChartError as e:
        sys.exit(f"エラー: {e}")
    for p in problems:
        print(p)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
