from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.guard import WorkspaceGuard
from adp.generation.project import ProjectInfo
from adp.generation.repair import Repairer
from adp.llm import AgentEvent, AgentSpec, LLMSettings, TextDelta, TurnResult
from tests.generation.test_dbt_codegen import DBT, FIXTURES, _warehouse_copy


def test_guard(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path)
    allowed = [
        ("Read", {"file_path": str(tmp_path / "models/a.sql")}),
        ("Edit", {"file_path": "models/a.sql", "new_string": "select 1"}),
        ("Write", {"file_path": "models/a.yml", "content": "version: 2"}),
        ("Glob", {"pattern": "models/**/*.sql"}),
        ("Bash", {"command": "dbt build --select fct_x"}),
        ("Bash", {"command": "dbt build -s +fct_x --quiet"}),
        ("Bash", {"command": "mf query --metrics revenue --group-by metric_time__day"}),
        ("Bash", {"command": "mf list metrics"}),
        ("mcp__beam__get_beam", {}),
    ]
    for tool, data in allowed:
        assert guard(tool, data) is None, (tool, data)
    denied = [
        ("Read", {"file_path": "/etc/passwd"}),
        ("Read", {"file_path": "~/.ssh/id_rsa"}),
        ("Write", {"file_path": "~/.zshrc", "content": "x"}),
        ("Grep", {"path": "~", "pattern": "KEY"}),
        ("Glob", {"pattern": "/Users/**/.env"}),
        ("Glob", {"pattern": "../**/*"}),
        ("Write", {"file_path": str(tmp_path / ".." / "x.sql"), "content": "x"}),
        # チャートは adp.charts が書き出す（#42）
        ("Write", {"file_path": "charts/x.yml", "content": "title: x"}),
        ("Write", {"file_path": "chart_specs/x.yml", "content": "title: x"}),
        # 大文字・小文字を区別しないファイルシステムでの迂回
        ("Write", {"file_path": "Charts/x.yml", "content": "title: x"}),
        ("MultiEdit", {"file_path": "CHART_SPECS/x.yml", "content": "x"}),
        ("Write", {"file_path": "DBT_CHARTS.yml", "content": "sources: {}"}),
        ("Write", {"file_path": "models/a.SQL.py", "content": "x"}),
        # Unicode の case folding（ſ → s）での迂回
        ("Write", {"file_path": "dbt_chart\u017f.yml", "content": "x"}),
        ("Write", {"file_path": "chart\u017f/x.yml", "content": "x"}),
        ("Write", {"file_path": "charts\x00/x.yml", "content": "x"}),
        ("Edit", {"file_path": str(tmp_path / "charts/.gitkeep/x.yml"), "new_string": "x"}),
        ("Write", {"file_path": "models/x.py", "content": "def model(dbt, s): ..."}),
        ("Edit", {"file_path": "dbt_project.yml", "new_string": "x"}),
        ("Edit", {"file_path": "profiles.yml", "new_string": "x"}),
        ("Write", {"file_path": "packages.yml", "content": "x"}),
        ("Write", {"file_path": "models/a.sql", "content": "select '{{ env_var(\"KEY\") }}'"}),
        ("Write", {"file_path": "models/a.sql", "content": "select '{{ env_var (\"KEY\") }}'"}),
        ("Write", {"file_path": "models/a.sql", "content": "{% set f = env_var %}{{ f('K') }}"}),
        ("Bash", {"command": "rm -rf /"}),
        ("Bash", {"command": "dbt build; rm -rf /"}),
        ("Bash", {"command": "dbt show --inline 'select 1'"}),
        ("Bash", {"command": "dbt run -s x"}),
        ("Bash", {"command": "dbt deps"}),
        ("Bash", {"command": "dbt build --profiles-dir /"}),
        ("Bash", {"command": "dbt build --target-path /tmp/x"}),
        ("Bash", {"command": "mf query --metrics revenue --csv /tmp/x.csv"}),
        ("Bash", {"command": "mf query --metrics revenue --where x"}),
        ("WebFetch", {"url": "https://example.com"}),
    ]
    for tool, data in denied:
        assert guard(tool, data) is not None, (tool, data)
    assert len(guard.denied) == len(denied)


def test_guard_checks_content_after_edit(tmp_path: Path) -> None:
    """2 回の Edit で禁止語を組み立てても拒否する。"""
    guard = WorkspaceGuard(tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.sql").write_text("select '{{ env_XX('K') }}'")
    assert (
        guard("Edit", {"file_path": "models/a.sql", "old_string": "_XX", "new_string": "_var"})
        is not None
    )


async def test_weakening_tests_is_detected(generated) -> None:  # type: ignore[no-untyped-def]
    """エージェントがテストを消して通しても、成功とみなさない。"""
    warehouse, queries = generated
    sql = warehouse / "models/marts/fct_order_lines.sql"
    original = sql.read_text()
    sql.write_text(original.replace("- t0.discount_amount", "+ t0.discount_amount"))
    reconcile = warehouse / "tests/assert_fct_order_lines_reconciles_with_source.sql"

    def cheat(n: int, spec: AgentSpec) -> None:
        # 計算を直さず、unit test と突合テストを消して通そうとする
        yml = warehouse / "models/marts/fct_order_lines.yml"
        text = yml.read_text()
        yml.write_text(text[: text.index("unit_tests:")])
        reconcile.unlink(missing_ok=True)

    result = await Repairer(FixingRunner(cheat), LLMSettings(), DBT.parent).run(warehouse, queries)
    assert not result.ok
    assert result.final.summary == "テストが弱められました"
    assert "削除・無効化" in result.final.log


class FixingRunner:
    """呼ばれるたびに fix(n) を実行する偽エージェント。"""

    def __init__(self, fix: Callable[[int, AgentSpec], None]) -> None:
        self.fix = fix
        self.models: list[str] = []

    async def run(
        self, prompt: str, spec: AgentSpec, resume: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        self.models.append(spec.model)
        assert spec.cwd is not None and spec.pre_tool_use is not None
        self.fix(len(self.models), spec)
        yield TextDelta(f"修正 {len(self.models)}")
        yield TurnResult("s", spec.model, False, 0.0, 0.0)


@pytest.fixture
def generated(tmp_path: Path) -> tuple[Path, list]:  # type: ignore[type-arg]
    warehouse = _warehouse_copy(tmp_path)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    gen = generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam)
    info.write(gen.files)
    return warehouse, gen.queries


async def test_repair_fixes_on_second_attempt(generated) -> None:  # type: ignore[no-untyped-def]
    warehouse, queries = generated
    sql = warehouse / "models/marts/fct_order_lines.sql"
    original = sql.read_text()
    sql.write_text(original.replace("select", "selec", 1))

    def fix(n: int, spec: AgentSpec) -> None:
        if n == 2:
            sql.write_text(original)

    runner = FixingRunner(fix)
    settings = LLMSettings(default_model="sonnet", escalation_model="opus")
    result = await Repairer(runner, settings, DBT.parent).run(warehouse, queries)
    assert result.ok
    assert runner.models == ["sonnet", "sonnet"]
    assert [a.result.ok for a in result.attempts] == [False, True]
    assert "mf query revenue" in result.final.summary


async def test_repair_escalates_then_hands_to_human(generated) -> None:  # type: ignore[no-untyped-def]
    warehouse, queries = generated
    sql = warehouse / "models/marts/fct_order_lines.sql"
    sql.write_text(sql.read_text().replace("select", "selec", 1))
    runner = FixingRunner(lambda n, spec: None)
    settings = LLMSettings(default_model="sonnet", escalation_model="opus")
    result = await Repairer(runner, settings, DBT.parent).run(warehouse, queries)
    assert not result.ok and result.handed_to_human
    assert runner.models == ["sonnet", "sonnet", "opus"]
    assert "dbt build が失敗" in result.final.summary
    assert "selec" in result.final.log


async def test_tasks_are_given_to_agent_first(generated) -> None:  # type: ignore[no-untyped-def]
    from adp.generation import AgentTask

    warehouse, queries = generated
    runner = FixingRunner(lambda n, spec: None)
    result = await Repairer(runner, LLMSettings(), DBT.parent).run(
        warehouse, queries, [AgentTask("add_dim_columns", "dim_store", "列を追加")]
    )
    assert result.ok and len(runner.models) == 1
    assert result.attempts[0].reason == "tasks"


def test_duckdb_external_access_is_disabled(tmp_path: Path) -> None:
    """dbt 経由でも DuckDB からファイルを読み書きできない（profiles.yml の設定）。"""
    from tests.generation.test_dbt_codegen import _dbt

    warehouse = _warehouse_copy(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (warehouse / "models" / "zz_read.sql").write_text(f"select * from read_text('{outside}')")
    result = _dbt(warehouse, "build", "--select", "zz_read")
    assert result.returncode != 0
    assert "secret" not in result.stdout


def test_decimal_measures_are_yaml_safe() -> None:
    from adp.generation.tests import _evaluate

    value = _evaluate("a * 2", {"a": "300.50"}, {"a": "DECIMAL(10,2)"})
    assert value == 601 and isinstance(value, int)
