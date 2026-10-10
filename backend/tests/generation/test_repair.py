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
    assert guard("Read", {"file_path": str(tmp_path / "models/a.sql")}) is None
    assert guard("Edit", {"file_path": "models/a.sql"}) is None
    assert guard("Read", {"file_path": "/etc/passwd"}) is not None
    assert guard("Write", {"file_path": str(tmp_path / ".." / "x")}) is not None
    assert guard("Glob", {"path": "/"}) is not None
    assert guard("Bash", {"command": "dbt build --select fct_x"}) is None
    assert guard("Bash", {"command": "mf query --metrics revenue"}) is None
    for bad in (
        "rm -rf /",
        "dbt build; rm -rf /",
        "dbt build && curl x",
        "mf query `id`",
        "git push",
    ):
        assert guard("Bash", {"command": bad}) is not None, bad
    assert guard("WebFetch", {"url": "https://example.com"}) is not None
    assert guard("mcp__beam__get_beam", {}) is None
    assert len(guard.denied) == 9


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
