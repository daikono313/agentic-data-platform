"""dbt Charts から Semantic Layer（MetricFlow）の指標を使う（#41、design-doc O4 の案 (a)）。

チャートの元ファイル `warehouse/chart_specs/<board>.yml` には、SQL ではなく指標と切り口を書く。

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
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from adp.metricflow import literal
from adp.warehouse import DROP_ENV, tool_env

HEADER = (
    "# adp により生成（chart_specs/{name}.yml から `python -m adp.charts compile`）。編集しない\n"
)
SOURCE = "warehouse"
# 元ファイルは charts/ の外に置く（charts/ の下は dbt Charts がボードとして配信する）
SPECS_DIR = "chart_specs"
CHARTS_CONFIG = {"sources": {SOURCE: {"type": "dbt_profile", "profile": "adp", "target": "dev"}}}
MF_TIMEOUT_S = 120
RENDER_TIMEOUT_S = 180
MAX_SVG_BYTES = 3_000_000
"""描画画面の SVG の上限（切り口の値が多いと十数 MB になり、画面が重くなるため）。"""
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


_IDENT = re.compile(r"^[a-z_][a-z0-9_]*\Z")
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
    try:
        return literal(value)
    except ValueError as e:
        raise ChartError(str(e)) from e


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
            if data.get(key) is not None and not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(data[key])
            ):
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
        env=tool_env(warehouse),
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
    return _finish_sql("\n".join(lines[start:]).strip(), query)


def _finish_sql(sql: str, query: MetricQuery) -> str:
    """ボードに埋め込める形にする。DB ファイル名に依存しないよう、カタログ名は外す。"""
    # dbt Charts もボードの SQL を Jinja で展開する。テンプレートを残さない
    if re.search(r"\{[{%#]|[}%#]\}", sql):
        raise ChartError(
            f"MetricFlow の SQL にテンプレート記法が残っています（{' '.join(query.args())}）"
        )
    tree = sqlglot.parse_one(sql, read="duckdb")
    for table in tree.find_all(exp.Table):
        table.set("catalog", None)
    return tree.sql(dialect="duckdb", pretty=True)


class MetricFlowCompiler:
    """プロセス内の MetricFlow で SQL を作る（描画画面用。#43）。

    `target/semantic_manifest.json` だけを読み、DB には接続しない（DuckDB をロックしない）。
    出力は `mf query --explain` と同じ。manifest が更新されたら読み直す。
    """

    def __init__(self, warehouse: Path) -> None:
        self.warehouse = warehouse
        self._lock = threading.Lock()
        self._engine: Any = None
        self._mtime = 0.0

    def _get_engine(self) -> Any:
        from dbt_metricflow.cli.dbt_connectors.dbt_config_accessor import dbtArtifacts
        from metricflow.engine.metricflow_engine import MetricFlowEngine
        from metricflow.protocols.sql_client import SqlEngine
        from metricflow.sql.render.duckdb_renderer import DuckDbSqlPlanRenderer
        from metricflow_semantics.model.semantic_manifest_lookup import SemanticManifestLookup

        path = self.warehouse / "target" / "semantic_manifest.json"
        try:
            mtime = path.stat().st_mtime
        except OSError as e:
            raise ChartError(
                "Semantic Layer の定義がありません（dbt parse を実行してください）"
            ) from e
        if self._engine is None or mtime != self._mtime:

            class _NoConnection:
                """SQL を作るだけで、実行はしない。"""

                sql_engine_type = SqlEngine.DUCKDB
                sql_plan_renderer = DuckDbSqlPlanRenderer()

                def _refuse(self, *_: Any, **__: Any) -> Any:
                    raise ChartError("描画画面の MetricFlow は SQL を実行しません")

                query = execute = dry_run = _refuse

                def close(self) -> None:
                    pass

                def render_bind_parameter_key(self, key: str) -> str:
                    return "$" + key

            try:
                manifest = dbtArtifacts.build_semantic_manifest_from_dbt_project_root(
                    self.warehouse
                )
            except Exception as e:
                raise ChartError(f"Semantic Layer の定義を読めません: {e}") from e
            self._engine = MetricFlowEngine(
                semantic_manifest_lookup=SemanticManifestLookup(manifest),
                sql_client=_NoConnection(),
            )
            self._mtime = mtime
        return self._engine

    def sql(self, query: MetricQuery) -> str:
        from metricflow.engine.metricflow_engine import MetricFlowQueryRequest

        def _time(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        with self._lock:
            engine = self._get_engine()
            try:
                result = engine.explain(
                    mf_request=MetricFlowQueryRequest.create(
                        metric_names=query.metrics,
                        group_by_names=query.group_by or None,
                        order_by_names=query.order or None,
                        limit=query.limit,
                        where_constraints=query.filters or None,
                        time_constraint_start=_time(query.start_time),
                        time_constraint_end=_time(query.end_time),
                    )
                )
            except ChartError:
                raise
            except Exception as e:
                detail = " ".join(str(e).split())[:300]
                raise ChartError(
                    f"MetricFlow で SQL を作れませんでした（{' '.join(query.args())}）: {detail}"
                ) from e
        return _finish_sql(str(result.sql_statement.without_descriptions.sql).strip(), query)

    def dimensions(self, metric: str) -> list[str]:
        """`mf list dimensions --metrics <metric>` と同じ名前の一覧。"""
        with self._lock:
            engine = self._get_engine()
            try:
                dims = engine.simple_dimensions_for_metrics([metric])
            except Exception as e:
                raise ChartError(f"{metric} の切り口を取れません: {e}") from e
        return [str(d.granularity_free_dunder_name) for d in dims]


class BoardRenderer:
    """dbt Charts をプロセス内で使い、ボードを SVG に描画する（描画画面用。#43）。

    接続は warehouse の dbt_charts.yml（dbt プロファイル経由。外部アクセスは無効）。描画のたびに
    接続を閉じ、DuckDB を開いたままにしない（dbt build を妨げない）。
    """

    def __init__(self, warehouse: Path) -> None:
        self.warehouse = warehouse
        self._lock = threading.Lock()
        self._session: Any = None

    def svg(self, board: dict[str, Any]) -> str:
        from dbt_charts.agent_api import InMemoryBoard, ProjectSession

        problems = _check_sources(self.warehouse)
        # プロセス内の dbt Charts は、環境変数でプロファイルや DB の場所が変わる
        # （CLI のように固定できない）
        for name in ("DBT_PROFILES_DIR", *DROP_ENV):
            if os.environ.get(name):
                problems.append(
                    f"環境変数 {name} を外して backend を起動してください"
                    "（描画が別の DB を見るため）"
                )
        if problems:
            raise ChartError(" / ".join(problems))
        text = yaml.safe_dump(board, allow_unicode=True, sort_keys=False)
        with self._lock:
            if self._session is None:
                self._session = ProjectSession.open(self.warehouse)
            try:
                result = self._session.render_board(
                    InMemoryBoard(text, path=None), format="svg", use_cache=False
                )
            except Exception as e:
                raise ChartError(f"描画できませんでした: {e}") from e
            finally:
                self._session.refresh()
        errors = [*result.validation_errors, *result.chart_errors]
        if result.board_error is not None:
            errors.insert(0, result.board_error)
        if result.status != "ok" or not isinstance(result.data, str):
            detail = " / ".join(str(getattr(e, "message", e)) for e in errors[:3])
            raise ChartError(f"描画できませんでした: {detail or result.status}")
        if len(result.data) > MAX_SVG_BYTES:
            raise ChartError(
                "チャートが大きすぎます（切り口の値が多すぎます）。粒度を粗くするか、期間を絞るか、"
                "別の切り口を選んでください"
            )
        return result.data


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
    for q in queries:
        if not isinstance(q, str) or not _IDENT.match(q):
            raise ChartError(f"{name}: metric_queries の名前は英小文字・数字・_ で書いてください")
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
        sort = chart.get("sort") or {}
        if ("by" in sort and not (isinstance(sort["by"], str) and _IDENT.match(sort["by"]))) or (
            "order" in sort and sort["order"] not in ("asc", "desc")
        ):
            raise ChartError(f"{p}.sort は by に列名、order に asc / desc を書いてください")
        if "style" in chart and (
            not isinstance(chart["style"], dict) or set(chart["style"]) - _STYLE_KEYS
        ):
            raise ChartError(f"{p}.style は number_format だけです")
        fmt = (chart.get("style") or {}).get("number_format")
        if fmt is not None and not (
            isinstance(fmt, str) and re.fullmatch(r"[0-9a-zA-Z$,.~%+\-( ]{1,20}", fmt)
        ):
            raise ChartError(f"{p}.style.number_format が不正です")
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


def build_board(name: str, spec: Any, sql_for: Callable[[MetricQuery], str]) -> dict[str, Any]:
    """元ファイルを検査し、各クエリを MetricFlow の SQL にしたボード（辞書）を作る。"""
    spec = _validate_spec(name, spec)
    board: dict[str, Any] = {}
    if "title" in spec:
        board["title"] = spec["title"]
    board["source"] = SOURCE
    board["queries"] = {
        q: {"sql": sql_for(MetricQuery.parse(q, data))}
        for q, data in spec["metric_queries"].items()
    }
    board["charts"] = spec["charts"]
    for key in ("rows", "cols"):
        if key in spec:
            board[key] = spec[key]
    return board


def compile_board(name: str, spec: Any, warehouse: Path, mf: Path) -> str:
    board = build_board(name, spec, lambda q: compile_sql(warehouse, mf, q))
    body = yaml.safe_dump(board, allow_unicode=True, sort_keys=False, width=100)
    return HEADER.format(name=name) + body


def _specs(warehouse: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted((warehouse / SPECS_DIR).glob("*.yml"))}


def _stray_files(root: Path, label: str) -> list[str]:
    """root の直下の `<名前>.yml` 以外（サブディレクトリ・シンボリックリンク・他の拡張子）を返す。

    dbt Charts は charts/ の下の .yml / .yaml / .md / .markdown をすべて配信し、
    シンボリックリンクもたどるため、許可するものだけを数える（#55 のレビュー）。
    """
    if root.is_symlink() or not root.is_dir():
        return []  # charts/ がディレクトリでないことは _check_sources が報告する
    problems = []
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        ok = (
            not entry.is_symlink()
            and entry.is_file(follow_symlinks=False)
            and entry.name.endswith(".yml")
            and _IDENT.match(entry.name.removesuffix(".yml")) is not None
        )
        if not ok:
            problems.append(
                f"{label}/{entry.name}: {label}/ の直下には <名前>.yml だけを置けます"
                "（サブディレクトリ・シンボリックリンク・他の形式は不可）"
            )
    return problems


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
    problems += _stray_files(charts_dir, "charts") + _stray_files(warehouse / SPECS_DIR, SPECS_DIR)
    boards = [
        p for p in sorted(charts_dir.glob("*.yml")) if not p.is_symlink() and _IDENT.match(p.stem)
    ]
    for path in boards:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("# adp により生成"):
            problems.append(
                f"{path.name}: 手書きのボードは置けません（chart_specs/ に元ファイルを書く）"
            )
        elif path.stem not in expected:
            problems.append(f"{path.name}: 元ファイル chart_specs/{path.stem}.yml がありません")
        elif text != expected[path.stem]:
            problems.append(f"{path.name}: 古くなっています（python -m adp.charts compile を実行）")
    for name in expected:
        if not (warehouse / "charts" / f"{name}.yml").exists():
            problems.append(
                f"{name}.yml: まだ書き出されていません（python -m adp.charts compile を実行）"
            )
    return problems


def render_png(warehouse: Path, dct: Path, board: str, out: Path) -> None:
    """ボードを PNG に描画する（PR 本文に載せる静的出力。描画できることの検証も兼ねる）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            str(dct),
            "render",
            f"charts/{board}.yml",
            "--format",
            "png",
            "-o",
            str(out),
            "--no-cache",
        ],
        cwd=warehouse,
        env=tool_env(warehouse),
        capture_output=True,
        text=True,
        check=False,
        timeout=RENDER_TIMEOUT_S,
    )
    if result.returncode != 0 or not out.exists():
        detail = " ".join((result.stderr or result.stdout).split())[-300:]
        raise ChartError(f"{board} を描画できませんでした: {detail}")


def _check_sources(warehouse: Path) -> list[str]:
    """dbt Charts の設定は、dbt プロファイル経由の warehouse だけを使う内容と完全に一致させる。

    独自の duckdb ソース・HTML・地図・キャッシュの場所などで外部への経路を戻せるため（ADR 0002）。
    `charts/` が無いと dbt Charts は `faces/` を配信するので、それも確かめる。
    """
    problems = []
    charts_dir = warehouse / "charts"
    if charts_dir.is_symlink() or not charts_dir.is_dir():
        problems.append("charts/ はディレクトリで置いてください")
    if (warehouse / "faces").exists() or (warehouse / "faces").is_symlink():
        problems.append("faces/ は置けません（dbt Charts がボードとして配信する）")
    path = warehouse / "dbt_charts.yml"
    if not path.exists():
        return [*problems, "dbt_charts.yml がありません"]
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if config != CHARTS_CONFIG:
        problems.append(f"dbt_charts.yml: 内容は {CHARTS_CONFIG} だけにしてください")
    return problems


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
