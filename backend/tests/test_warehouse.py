"""接続先の設定（#81）。いまは DuckDB だけ。"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from adp.warehouse import UnsupportedWarehouse, tool_env, warehouse_db, warehouse_kind


def test_default_is_duckdb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ADP_WAREHOUSE", raising=False)
    monkeypatch.delenv("ADP_DUCKDB_PATH", raising=False)
    assert warehouse_kind(tmp_path / ".env") == "duckdb"
    assert warehouse_db(tmp_path / "warehouse") == tmp_path / "warehouse" / "data" / "dev.duckdb"


def test_unsupported_kind_stops_with_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ADP_WAREHOUSE", raising=False)
    monkeypatch.delenv("ADP_DUCKDB_PATH", raising=False)
    env = tmp_path / ".env"
    env.write_text("ADP_WAREHOUSE=bigquery\n")
    with pytest.raises(UnsupportedWarehouse, match="0005-real-data"):
        warehouse_kind(env)
    monkeypatch.setenv("ADP_WAREHOUSE", "DuckDB")
    with pytest.raises(UnsupportedWarehouse):
        warehouse_kind(env)  # ほかの設定と同じく .env が優先
    assert warehouse_kind(tmp_path / "none.env") == "duckdb"


@pytest.mark.parametrize(
    "line",
    ["export ADP_WAREHOUSE=bigquery", "ADP_WAREHOUSE=bigquery # 本番", "ADP_WAREHOUSE='bigquery'"],
)
def test_dotenv_forms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, line: str) -> None:
    """export・行末のコメント・引用符の書き方でも読み違えない（#96 レビュー）。"""
    monkeypatch.delenv("ADP_WAREHOUSE", raising=False)
    monkeypatch.delenv("ADP_DUCKDB_PATH", raising=False)
    env = tmp_path / ".env"
    env.write_text(line + "\n")
    with pytest.raises(UnsupportedWarehouse, match="bigquery"):
        warehouse_kind(env)


def test_duckdb_path_override_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """dbt だけが別の DB を見る設定では起動しない（#96 レビュー）。"""
    monkeypatch.delenv("ADP_WAREHOUSE", raising=False)
    monkeypatch.setenv("ADP_DUCKDB_PATH", "/tmp/other.duckdb")
    with pytest.raises(UnsupportedWarehouse, match="ADP_DUCKDB_PATH"):
        warehouse_kind(tmp_path / ".env")


def test_tool_env_drops_db_override_and_fixes_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """子プロセスの dbt / mf / dct は、いつも warehouse のプロファイルと DB を見る（#110）。"""
    monkeypatch.setenv("ADP_DUCKDB_PATH", "/tmp/real.duckdb")
    monkeypatch.setenv("DBT_PROFILES_DIR", "/elsewhere")
    monkeypatch.setenv("ADP_TEST_KEEP", "1")
    env = tool_env(tmp_path)
    assert "ADP_DUCKDB_PATH" not in env
    assert env["DBT_PROFILES_DIR"] == str(tmp_path) and env["NO_COLOR"] == "1"
    assert env["ADP_TEST_KEEP"] == "1"  # ほかの変数（PATH・認証など）はそのまま渡す


def test_every_tool_subprocess_uses_tool_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """dbt / mf / dct / 検査を子プロセスで動かす箇所は、どれも tool_env を使う（#110）。"""
    from adp import charts, sync
    from adp.generation import repair

    monkeypatch.setenv("ADP_DUCKDB_PATH", "/tmp/real.duckdb")
    seen: list[dict[str, str]] = []

    def fake_run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, "SELECT 1", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    query = charts.MetricQuery(["m"], [], [], None, [], None, None)
    charts.compile_sql(tmp_path, tmp_path / "mf", query)
    with pytest.raises(charts.ChartError):  # PNG は書かれないので失敗するが、呼び出しは見える
        charts.render_png(tmp_path, tmp_path / "dct", "b", tmp_path / "out.png")
    repair._run(["dbt", "build"], tmp_path)
    sync._check(["python", "-m", "adp.charts", "check"], tmp_path / "backend", tmp_path)
    sync._check([str(tmp_path / "mf"), "validate-configs"], tmp_path / "backend", tmp_path)
    assert len(seen) == 5
    assert all(env == tool_env(tmp_path) for env in seen)


def test_tool_env_is_the_only_place_that_builds_the_env() -> None:
    """新しく子プロセスを足しても、環境変数を自前で組み立てない（tool_env を使う。#110）。"""
    src = Path(__file__).parents[1] / "src" / "adp"
    offenders = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if p.name != "warehouse.py"
        and (
            '"NO_COLOR"' in (text := p.read_text(encoding="utf-8")) or '"DBT_PROFILES_DIR":' in text
        )
    ]
    assert offenders == []
