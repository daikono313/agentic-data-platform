"""接続先の設定（#81）。いまは DuckDB だけ。"""

from pathlib import Path

import pytest

from adp.warehouse import UnsupportedWarehouse, warehouse_db, warehouse_kind


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
