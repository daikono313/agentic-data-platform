"""データ基盤（warehouse）の接続先（#81）。

いまは DuckDB のファイル（`warehouse/data/dev.duckdb`）だけに対応する。接続先の決め方をここに
集めておき、実データ（BigQuery など）への切り替えはここと、dbt のプロファイル・カタログ・
MetricFlow・dbt Charts の接続を差し替える（手順は docs/adr/0005-real-data.md）。
"""

from __future__ import annotations

import os
from pathlib import Path

from adp.llm.settings import read_dotenv

SUPPORTED = ("duckdb",)


class UnsupportedWarehouse(RuntimeError):
    pass


def warehouse_kind(env_file: Path | None = None) -> str:
    """`ADP_WAREHOUSE`（.env か環境変数。ほかの設定と同じく .env が優先）。未設定なら duckdb。

    未対応の値や、アプリと dbt で別の DB を見ることになる設定（ADP_DUCKDB_PATH）はエラーにする。
    """
    if os.environ.get("ADP_DUCKDB_PATH"):
        raise UnsupportedWarehouse(
            "環境変数 ADP_DUCKDB_PATH を外して起動してください（dbt だけが別の DB を見るため。"
            "アプリの DB は warehouse/data/dev.duckdb）"
        )
    values = read_dotenv(env_file) if env_file is not None else {}
    kind = values.get("ADP_WAREHOUSE") or os.environ.get("ADP_WAREHOUSE", "")
    kind = (kind or "duckdb").lower()
    if kind not in SUPPORTED:
        raise UnsupportedWarehouse(
            f"ADP_WAREHOUSE={kind} にはまだ対応していません（対応: {', '.join(SUPPORTED)}。"
            "切り替えの手順は docs/adr/0005-real-data.md）"
        )
    return kind


def warehouse_db(warehouse: Path) -> Path:
    """DuckDB のファイル（dbt のプロファイル `warehouse/profiles.yml` の既定と同じ場所）。

    profiles.yml は ADP_DUCKDB_PATH で場所を変えられるが、アプリはその指定を受け付けない
    （warehouse_kind が起動時に止める）。子プロセスの dbt / mf / dct にも渡さない（tool_env）。
    """
    return warehouse / "data" / "dev.duckdb"


DROP_ENV = ("ADP_DUCKDB_PATH",)
"""dbt のプロファイルの DB の場所を変える環境変数。子プロセスには渡さない（設定されていると、
生成の検証が作業ツリーではなく手元の本物の DB に書き、描画・反映が別の DB を見る）。"""


def tool_env(warehouse: Path) -> dict[str, str]:
    """dbt / mf / dct（と検査の `python -m adp.*`）を子プロセスで動かすときの環境変数。

    プロファイルは `warehouse` のもの（DBT_PROFILES_DIR）、出力に色を付けない（NO_COLOR）、
    DB の場所を変える変数（DROP_ENV）は外す。どの呼び出しもここを通す（#110）。
    """
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    return env | {"DBT_PROFILES_DIR": str(warehouse), "NO_COLOR": "1"}
