"""描画画面（#43）: 指標・切り口・粒度・期間から、プロセス内で SVG を描く。"""

import sqlite3
from datetime import date
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from adp.api.app import create_app
from adp.api.service import SessionService
from adp.api.store import Store
from adp.chart_view import ChartRequest, ChartView, ChartViews
from adp.charts import ChartError, MetricQuery, compile_sql
from adp.interview import Interviewer
from adp.llm import LLMSettings, ScriptedRunner
from tests.test_charts import MF
from tests.test_dbt_charts import warehouse  # noqa: F401  （fixture。dbt build 済み）


@pytest.fixture(scope="module")
def view(warehouse: Path) -> ChartView:  # noqa: F811
    return ChartView(warehouse.parent)


def test_line_by_month_and_channel(view: ChartView, warehouse: Path) -> None:  # noqa: F811
    result = view.render(ChartRequest(metric="revenue", dimension="store__channel", grain="month"))
    assert result.kind == "line"
    assert result.svg.lstrip().startswith("<svg")
    assert "DATE_TRUNC('MONTH'" in result.sql.upper().replace('"', "'")
    assert "store__channel" in result.sql
    # 描画のあと DuckDB を開いたままにしない（dbt build を妨げない）
    duckdb.connect(str(warehouse / "data" / "dev.duckdb")).close()


def test_bar_and_kpi(view: ChartView) -> None:
    bar = view.render(ChartRequest(metric="revenue", dimension="store__store_name", grain=None))
    assert bar.kind == "bar" and "LIMIT 20" in bar.sql
    kpi = view.render(ChartRequest(metric="revenue", grain=None))
    assert kpi.kind == "kpi" and kpi.svg.lstrip().startswith("<svg")


@pytest.mark.parametrize(
    "query",
    [
        {"metrics": ["revenue"], "group_by": ["metric_time__month", "store__channel"]},
        {
            "metrics": ["revenue"],
            "group_by": ["store__store_name"],
            "order": ["-revenue"],
            "limit": 20,
        },
        {"metrics": ["revenue"], "start_time": "2024-01-01", "end_time": "2024-03-31"},
        {
            "metrics": ["revenue"],
            "group_by": ["metric_time__week"],
            "filters": [{"dimension": "store__channel", "op": "=", "value": "app"}],
        },
    ],
)
def test_in_process_sql_matches_mf_cli(
    view: ChartView,
    warehouse: Path,  # noqa: F811
    query: dict[str, object],
) -> None:
    """プロセス内の MetricFlow は `mf query --explain` と同じ SQL を作る（ADR 0003）。"""
    q = MetricQuery.parse("q", query)
    assert view.index.compiler.sql(q) == compile_sql(warehouse, MF, q)


def test_env_vars_that_change_the_db_are_refused(
    view: ChartView, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADP_DUCKDB_PATH", "/tmp/other.duckdb")
    with pytest.raises(ChartError, match="ADP_DUCKDB_PATH"):
        view.render(ChartRequest(metric="revenue"))


def test_too_large_charts_are_refused(view: ChartView, monkeypatch: pytest.MonkeyPatch) -> None:
    from adp import charts

    monkeypatch.setattr(charts, "MAX_SVG_BYTES", 100)
    with pytest.raises(ChartError, match="大きすぎます"):
        view.render(ChartRequest(metric="revenue"))


def test_period_is_applied(view: ChartView) -> None:
    result = view.render(
        ChartRequest(metric="revenue", grain="week", start=date(2024, 1, 1), end=date(2024, 3, 31))
    )
    assert "2024-01-01" in result.sql and "2024-03-31" in result.sql


@pytest.mark.parametrize(
    ("req", "match"),
    [
        (ChartRequest(metric="no_such_metric"), "ありません"),
        (ChartRequest(metric="revenue", dimension="metric_time"), "切れません"),
        (ChartRequest(metric="revenue", dimension="store__channel; select 1"), "切れません"),
        (
            ChartRequest(metric="revenue", start=date(2024, 3, 1), end=date(2024, 1, 1)),
            "期間",
        ),
    ],
)
def test_invalid_requests(view: ChartView, req: ChartRequest, match: str) -> None:
    with pytest.raises(ChartError, match=match):
        view.render(req)


def test_branch_path_cannot_escape_worktrees(tmp_path: Path) -> None:
    views = ChartViews(tmp_path)
    for branch in ("../../etc", "beam/../../x", "missing"):
        with pytest.raises(ChartError):
            views.for_branch(branch)


def test_api(warehouse: Path, tmp_path: Path) -> None:  # noqa: F811
    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    store = Store(tmp_path / "app.db")
    service = SessionService(store, Interviewer(ScriptedRunner([]), LLMSettings()), noop)
    client = TestClient(create_app(service, None, ChartViews(warehouse.parent)))
    options = client.get("/charts/options").json()
    revenue = next(m for m in options["metrics"] if m["name"] == "revenue")
    assert any(d["name"] == "store__channel" for d in revenue["dimensions"])
    ok = client.post("/charts/render", json={"metric": "revenue", "grain": "month"})
    assert ok.status_code == 200 and ok.json()["svg"].lstrip().startswith("<svg")
    bad = client.post("/charts/render", json={"metric": "revenue", "grain": "hour"})
    assert bad.status_code == 422
    missing = client.post("/charts/render", json={"metric": "x"})
    assert missing.status_code == 422 and "ありません" in missing.json()["detail"]
    # PR の無いジョブはプレビューできない
    store.save_job("j1", "s1", "failed", [], None, "x", "beam/x")
    assert client.get("/charts/options", params={"job_id": "j1"}).status_code == 404


def test_job_preview_uses_worktree(warehouse: Path, tmp_path: Path) -> None:  # noqa: F811
    repo = tmp_path / "repo"
    worktree = repo / ".adp" / "worktrees" / "beam-x"
    worktree.mkdir(parents=True)
    (worktree / "warehouse").symlink_to(warehouse, target_is_directory=True)

    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    store = Store(tmp_path / "app.db")
    store.save_job("j2", "s1", "succeeded", [], "https://github.com/o/r/pull/1", None, "beam/x")
    service = SessionService(store, Interviewer(ScriptedRunner([]), LLMSettings()), noop)
    client = TestClient(create_app(service, None, ChartViews(repo)))
    options = client.get("/charts/options", params={"job_id": "j2"}).json()
    assert options["preview"] is True and any(m["name"] == "revenue" for m in options["metrics"])
    rendered = client.post(
        "/charts/render", params={"job_id": "j2"}, json={"metric": "revenue", "grain": "month"}
    )
    assert rendered.status_code == 200, rendered.text


def test_store_adds_branch_column_to_old_db(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE jobs (id VARCHAR PRIMARY KEY, session_id VARCHAR, status VARCHAR,"
        " steps_json VARCHAR, pr_url VARCHAR, error VARCHAR, created_at DATETIME,"
        " updated_at DATETIME)"
    )
    con.commit()
    con.close()
    store = Store(db)
    store.save_job("j", "s", "succeeded", [], "u", None, "beam/x")
    row = store.job("j")
    assert row is not None and row.branch == "beam/x"
