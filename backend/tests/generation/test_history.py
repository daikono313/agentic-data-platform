"""属性の履歴（snapshot と履歴モデル。ADR 0009）の生成。"""

from pathlib import Path

import yaml

from adp.design import Column, ColumnRole, DimTable, HistoryDesign, SourceTable
from adp.generation.dbt import Generation, _history, history_files


class _Info:
    def __init__(self, root: Path, columns: list[tuple[str, str]], models: dict[str, set[str]]):
        self.warehouse_dir = root
        self._columns = columns
        self._models = models

    def source_columns(self, table: str) -> list[tuple[str, str]]:
        return self._columns

    def existing_models(self) -> set[str]:
        return set(self._models)

    def existing_columns(self, model: str) -> set[str]:
        return self._models.get(model, set())


def _dim(*attrs: str) -> DimTable:
    columns = [
        Column(name=a, role=ColumnRole.ATTRIBUTE, expr=a, description=f"{a}のラベル", nullable=True)
        for a in attrs
    ]
    return DimTable(
        name="dim_customer",
        dimension="customer",
        label="顧客",
        key_column="customer_key",
        base=SourceTable(table="raw.customers", alias="t0", model="stg_customers"),
        columns=[
            Column(name="customer_id", role=ColumnRole.NATURAL_KEY, expr="t0.customer_id"),
            *[c.model_copy(update={"expr": f"t0.{c.expr}"}) for c in columns],
        ],
        history=HistoryDesign(
            model="dim_customer_history",
            snapshot="snp_customers",
            natural_key="customer_id",
            columns=columns,
        ),
    )


TYPES = [("customer_id", "INTEGER"), ("region", "VARCHAR"), ("rank", "INTEGER")]


def test_history_files(tmp_path: Path) -> None:
    files = {
        f.path: f.content
        for f in history_files(
            _dim("region", "rank"),
            _Info(tmp_path, [*TYPES, ("updated_at", "TIMESTAMP")], {}),  # type: ignore[arg-type]
            semantic=True,
        )
    }
    assert list(files) == [
        "snapshots/snp_customers.yml",
        "models/marts/dim_customer_history.sql",
        "models/marts/dim_customer_history.yml",
    ]
    snapshot = yaml.safe_load(files["snapshots/snp_customers.yml"])["snapshots"][0]
    assert snapshot["relation"] == "ref('stg_customers')"
    assert snapshot["config"] | {} == {
        "schema": "snapshots",
        "unique_key": "customer_id",
        "strategy": "timestamp",
        "updated_at": "updated_at",
    }
    sql = files["models/marts/dim_customer_history.sql"]
    assert "{{ ref('snp_customers') }}" in sql and "{{ ref('dim_date') }}" in sql
    # 不明メンバー: 文字列の属性は「不明」、それ以外は null
    assert "select md5('__unknown__'), {{ unknown_key() }}, '不明', null," in sql
    model = yaml.safe_load(files["models/marts/dim_customer_history.yml"])["models"][0]
    assert model["primary_entity"] == "customer_version"
    columns = {c["name"]: c for c in model["columns"]}
    assert columns["customer_key"]["entity"] == {"type": "natural", "name": "customer"}
    assert columns["region"]["dimension"] == {"type": "categorical", "name": "region_at_event"}
    assert columns["region"]["description"] == "regionのラベル（当時）"
    assert columns["valid_from"]["dimension"]["validity_params"] == {"is_start": True}
    assert columns["valid_to"]["dimension"]["validity_params"] == {"is_end": True}
    assert "dbt_utils.mutually_exclusive_ranges" in model["data_tests"][0]


def test_snapshot_without_updated_at_checks_all_columns(tmp_path: Path) -> None:
    """源泉に更新日時が無ければ、snapshot を実行した時刻で版を分ける（strategy: check）。"""
    files = history_files(_dim("region"), _Info(tmp_path, TYPES, {}))  # type: ignore[arg-type]
    config = yaml.safe_load(files[0].content)["snapshots"][0]["config"]
    assert config["strategy"] == "check" and config["check_cols"] == "all"


def test_existing_history_model_gets_missing_columns_by_agent(tmp_path: Path) -> None:
    """履歴モデルが既にあれば作り直さず、足りない属性をエージェントへの作業にする。同じ源泉の
    snapshot が既にあれば、それを使う。"""
    info = _Info(tmp_path, TYPES, {"dim_customer_history": {"region"}})
    result = Generation()
    assert _history(_dim("region", "rank"), info, info.existing_models(), True, result) == []  # type: ignore[arg-type]
    assert result.reused == ["dim_customer_history"]
    [task] = result.tasks
    assert task.kind == "add_history_columns" and "rank" in task.instruction

    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "snp_customers.yml").write_text("snapshots: []\n")
    info = _Info(tmp_path, TYPES, {})
    files = _history(_dim("region"), info, set(), True, Generation())  # type: ignore[arg-type]
    assert [f.path for f in files] == [
        "models/marts/dim_customer_history.sql",
        "models/marts/dim_customer_history.yml",
    ]
