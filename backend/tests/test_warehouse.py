"""接続先の設定（#81）。いまは DuckDB だけ。"""

from pathlib import Path

import pytest

from adp.warehouse import UnsupportedWarehouse, warehouse_db, warehouse_kind


def test_default_is_duckdb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ADP_WAREHOUSE", raising=False)
    assert warehouse_kind(tmp_path / ".env") == "duckdb"
    assert warehouse_db(tmp_path / "warehouse") == tmp_path / "warehouse" / "data" / "dev.duckdb"


def test_unsupported_kind_stops_with_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ADP_WAREHOUSE", raising=False)
    env = tmp_path / ".env"
    env.write_text("ADP_WAREHOUSE=bigquery\n")
    with pytest.raises(UnsupportedWarehouse, match="0005-real-data"):
        warehouse_kind(env)
    monkeypatch.setenv("ADP_WAREHOUSE", "DuckDB")
    assert warehouse_kind(env) == "duckdb"  # 環境変数が優先
