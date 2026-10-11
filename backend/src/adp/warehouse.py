"""データ基盤（warehouse）の接続先（#81）。

いまは DuckDB のファイル（`warehouse/data/dev.duckdb`）だけに対応する。接続先の決め方をここに
集めておき、実データ（BigQuery など）への切り替えはここと、dbt のプロファイル・カタログ・
MetricFlow・dbt Charts の接続を差し替える（手順は docs/adr/0005-real-data.md）。
"""

from __future__ import annotations

import os
from pathlib import Path

SUPPORTED = ("duckdb",)


class UnsupportedWarehouse(RuntimeError):
    pass


def warehouse_kind(env_file: Path | None = None) -> str:
    """`ADP_WAREHOUSE`（環境変数か .env）。未設定なら duckdb。未対応の値はエラーにする。"""
    kind = os.environ.get("ADP_WAREHOUSE", "")
    if not kind and env_file is not None and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "ADP_WAREHOUSE":
                kind = value.strip().strip("'\"")
    kind = (kind or "duckdb").lower()
    if kind not in SUPPORTED:
        raise UnsupportedWarehouse(
            f"ADP_WAREHOUSE={kind} にはまだ対応していません（対応: {', '.join(SUPPORTED)}。"
            "切り替えの手順は docs/adr/0005-real-data.md）"
        )
    return kind


def warehouse_db(warehouse: Path) -> Path:
    """DuckDB のファイル（dbt のプロファイル `warehouse/profiles.yml` と同じ場所）。"""
    return warehouse / "data" / "dev.duckdb"
