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
_QUERY_KEYS = {"metrics", "group_by", "order", "limit", "filters", "start_time", "end_time"}
# 元ファイルで使えるキー（許可リスト）。
# dbt Charts の他の機能（extends、ボードの入れ子、HTML、地図など）は使わない
_TOP_KEYS = {"title", "metric_queries", "charts", "rows", "cols"}
_CHART_TYPES = {
    "bar",
    "line",
    "area",
    "scatter",
    "heatmap",
    "histogram",
    "pie",
    "donut",
    "kpi",
    "table",
}
_CHART_KEYS = {
    "type",
    "query",
    "title",
    "x",
    "y",
    "color",
    "x_label",
    "y_label",
    "sort",
    "value",
    "label",
    "style",
}
_SORT_KEYS = {"by", "order"}
_STYLE_KEYS = {"number_format"}
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


_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_OPS = {
    "=": "=",
    "!=": "<>",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
    "in": "in",
    "not_in": "not in",
}


def _idents(name: str, key: str, value: Any, allow_desc: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ChartError(f"metric_queries.{name}.{key} は文字列のリストで書いてください")
    for v in value:
        if not _IDENT.match(v.removeprefix("-") if allow_desc else v):
            raise ChartError(f"metric_queries.{name}.{key} に使えない名前があります: {v!r}")
    return list(value)


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise ChartError(f"filter の値に使えない型があります: {value!r}")


def _filter_expr(name: str, f: Any) -> str:
    """構造化した絞り込み条件を MetricFlow の where に変換する（任意の SQL を書かせない）。"""
    if not isinstance(f, dict) or set(f) != {"dimension", "op", "value"}:
        raise ChartError(
            f"metric_queries.{name}.filters は {{dimension, op, value}} で書いてください"
        )
    dim, op, value = f["dimension"], f["op"], f["value"]
    if not isinstance(dim, str) or not _IDENT.match(dim) or "__" not in dim:
        raise ChartError(f"metric_queries.{name}.filters の dimension が不正です: {dim!r}")
    if op not in _OPS:
        raise ChartError(f"metric_queries.{name}.filters の op は {sorted(_OPS)} のどれかです")
    ref = "{{ Dimension('" + dim + "') }}"
    if op in ("in", "not_in"):
        if not isinstance(value, list) or not value:
            raise ChartError(f"metric_queries.{name}.filters の {op} にはリストを指定してください")
        return f"{ref} {_OPS[op]} ({', '.join(_literal(v) for v in value)})"
    return f"{ref} {_OPS[op]} {_literal(value)}"


@dataclass
class MetricQuery:
    metrics: list[str]
    group_by: list[str]
    order: list[str]
    limit: int | None
    filters: list[str]
    start_time: str | None
    end_time: str | None

    @classmethod
    def parse(cls, name: str, data: Any) -> MetricQuery:
        if not isinstance(data, dict):
            raise ChartError(f"metric_queries.{name} は辞書で書いてください")
        unknown = set(data) - _QUERY_KEYS
        if unknown:
            raise ChartError(f"metric_queries.{name} に使えないキーがあります: {sorted(unknown)}")
        if not data.get("metrics"):
            raise ChartError(f"metric_queries.{name} に metrics がありません")
        limit = data.get("limit")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ChartError(f"metric_queries.{name}.limit は正の整数で書いてください")
        for key in ("start_time", "end_time"):
            if data.get(key) is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data[key])):
                raise ChartError(f"metric_queries.{name}.{key} は YYYY-MM-DD で書いてください")
        return cls(
            metrics=_idents(name, "metrics", data["metrics"]),
            group_by=_idents(name, "group_by", data.get("group_by") or []),
            order=_idents(name, "order", data.get("order") or [], allow_desc=True),
            limit=limit,
            filters=[_filter_expr(name, f) for f in (data.get("filters") or [])],
            start_time=str(data["start_time"]) if data.get("start_time") else None,
            end_time=str(data["end_time"]) if data.get("end_time") else None,
        )

    def args(self) -> list[str]:
        args = ["--metrics", ",".join(self.metrics)]
        if self.group_by:
            args += ["--group-by", ",".join(self.group_by)]
        if self.order:
            args += ["--order", ",".join(self.order)]
        if self.limit is not None:
            args += ["--limit", str(self.limit)]
        for f in self.filters:
            args += ["--where", f]
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
        detail = " ".join((result.stderr or result.stdout).split())[-300:]
        raise ChartError(
            f"MetricFlow で SQL を作れませんでした（{' '.join(query.args())}）: {detail}"
        )
    sql = "\n".join(lines[start:]).strip()
    tree = sqlglot.parse_one(sql, read="duckdb")
    for table in tree.find_all(exp.Table):
        table.set("catalog", None)
    return tree.sql(dialect="duckdb", pretty=True)


def _validate_spec(name: str, spec: Any) -> dict[str, Any]:
    """元ファイルを許可リストで検査する。SQL・外部への経路・他ファイルの読み込みを入れない。"""
    if not isinstance(spec, dict):
        raise ChartError(f"{name}: 辞書で書いてください")
    unsafe = _unsafe(spec)
    if unsafe:
        raise ChartError(f"{name}: " + " / ".join(unsafe))
    unknown = set(spec) - _TOP_KEYS
    if unknown:
        raise ChartError(
            f"{name}: 使えないキーがあります: {sorted(unknown)}（SQL や extends は書けません）"
        )
    queries = spec.get("metric_queries")
    if not isinstance(queries, dict) or not queries:
        raise ChartError(f"{name}: metric_queries がありません")
    charts = spec.get("charts")
    if not isinstance(charts, dict) or not charts:
        raise ChartError(f"{name}: charts がありません")
    for cname, chart in charts.items():
        p = f"{name}: charts.{cname}"
        if not isinstance(cname, str) or not _IDENT.match(cname):
            raise ChartError(f"{p}: チャート名は英小文字・数字・_ で書いてください")
        if not isinstance(chart, dict):
            raise ChartError(f"{p} は辞書で書いてください")
        if set(chart) - _CHART_KEYS:
            raise ChartError(f"{p} に使えないキーがあります: {sorted(set(chart) - _CHART_KEYS)}")
        if chart.get("type") not in _CHART_TYPES:
            raise ChartError(f"{p}.type は {sorted(_CHART_TYPES)} のどれかです")
        if not isinstance(chart.get("query"), str) or chart["query"] not in queries:
            raise ChartError(
                f"{p}.query は metric_queries の名前で書いてください（SQL は書けません）"
            )
        for key in ("x", "y", "color", "value"):
            v = chart.get(key)
            if v is not None and (not isinstance(v, str) or not _IDENT.match(v)):
                raise ChartError(f"{p}.{key} は列名で書いてください")
        for key in ("title", "x_label", "y_label", "label"):
            if key in chart and (
                not isinstance(chart[key], str) or re.search(r"[<>{}]", chart[key])
            ):
                raise ChartError(f"{p}.{key} に使えない文字（< > {{ }}）があります")
        if "sort" in chart and (
            not isinstance(chart["sort"], dict) or set(chart["sort"]) - _SORT_KEYS
        ):
            raise ChartError(f"{p}.sort は by と order だけです")
        if "style" in chart and (
            not isinstance(chart["style"], dict) or set(chart["style"]) - _STYLE_KEYS
        ):
            raise ChartError(f"{p}.style は number_format だけです")
    if "title" in spec and (
        not isinstance(spec["title"], str) or re.search(r"[<>{}]", spec["title"])
    ):
        raise ChartError(f"{name}: title に使えない文字（< > {{ }}）があります")
    for key in ("rows", "cols"):
        if key in spec:
            _check_layout(name, spec[key], set(charts))
    if "rows" not in spec and "cols" not in spec:
        raise ChartError(f"{name}: rows か cols でレイアウトを書いてください")
    return spec


def _check_layout(name: str, layout: Any, charts: set[str]) -> None:
    """レイアウトはチャート名と、rows / cols の入れ子だけ（ボードの入れ子や HTML は不可）。"""
    if not isinstance(layout, list):
        raise ChartError(f"{name}: レイアウトはリストで書いてください")
    for item in layout:
        if isinstance(item, str):
            if item not in charts:
                raise ChartError(f"{name}: レイアウトのチャート {item} がありません")
        elif isinstance(item, dict) and len(item) == 1 and set(item) <= {"rows", "cols"}:
            _check_layout(name, next(iter(item.values())), charts)
        else:
            raise ChartError(f"{name}: レイアウトにはチャート名と rows / cols だけを書けます")


def compile_board(name: str, spec: Any, warehouse: Path, mf: Path) -> str:
    spec = _validate_spec(name, spec)
    board: dict[str, Any] = {}
    if "title" in spec:
        board["title"] = spec["title"]
    board["source"] = SOURCE
    board["queries"] = {
        q: {"sql": compile_sql(warehouse, mf, MetricQuery.parse(q, data))}
        for q, data in spec["metric_queries"].items()
    }
    board["charts"] = spec["charts"]
    for key in ("rows", "cols"):
        if key in spec:
            board[key] = spec[key]
    body = yaml.safe_dump(board, allow_unicode=True, sort_keys=False, width=100)
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
    charts_dir = warehouse / "charts"
    boards = [
        p
        for p in sorted([*charts_dir.rglob("*.yml"), *charts_dir.rglob("*.yaml")])
        if (charts_dir / "metrics") not in p.parents
    ]
    for path in boards:
        if path.parent != charts_dir or path.suffix != ".yml":
            problems.append(
                f"{path.relative_to(charts_dir)}: ボードは charts/ の直下に .yml で置きます"
            )
            continue
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
