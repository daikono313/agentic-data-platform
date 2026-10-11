"""生成の自己修復の eval（#78）の仕組み。LLM は偽物（実際の LLM は make eval-repair）。"""

from pathlib import Path

import pytest

from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.evals.repair import (
    SCENARIO_DIR,
    Fault,
    RepairScenario,
    prepare_warehouse,
    run_scenario,
    summary,
)
from adp.generation import generate_models
from adp.generation.project import ProjectInfo
from adp.llm import AgentSpec, LLMSettings
from adp.llm.settings import REPO_ROOT
from tests.generation.test_dbt_codegen import DBT
from tests.generation.test_repair import FixingRunner

SCENARIOS = [RepairScenario.load(p) for p in sorted(SCENARIO_DIR.glob("*.yml"))]


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """生成した直後のファイル（シナリオの失敗が、今の生成物に入れられるかを確かめる）。"""
    warehouse = prepare_warehouse(tmp_path_factory.mktemp("repair"))
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    out: dict[str, str] = {}
    for beam_path in {s.beam for s in SCENARIOS}:
        beam = load(REPO_ROOT / beam_path)
        gen = generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam)
        out |= {f.path: f.content for f in gen.files}
    return out


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_fault_applies_to_current_generation(
    scenario: RepairScenario, generated: dict[str, str], tmp_path: Path
) -> None:
    """生成器が変わってシナリオの失敗が入らなくなったら気づく（古いシナリオで成功率を測らない）。"""
    path = tmp_path / scenario.fault.file
    path.parent.mkdir(parents=True)
    path.write_text(generated[scenario.fault.file], encoding="utf-8")
    scenario.fault.apply(tmp_path)
    assert path.read_text(encoding="utf-8") != generated[scenario.fault.file]


def test_fault_rejects_paths_outside_and_stale_text(tmp_path: Path) -> None:
    (tmp_path / "a.sql").write_text("select 1")
    with pytest.raises(ValueError, match="外"):
        Fault(file="../x.sql", replace=("a", "b")).apply(tmp_path)
    with pytest.raises(ValueError, match="シナリオが古い"):
        Fault(file="a.sql", replace=("from", "frm")).apply(tmp_path)


async def test_harness_measures_fixed_and_not_fixed(tmp_path: Path) -> None:
    """失敗を入れ、直すエージェントなら「期待どおり」、直さなければ「期待と違う」になる。"""
    scenario = next(s for s in SCENARIOS if s.name == "sql_typo")

    def undo(n: int, spec: AgentSpec) -> None:
        assert spec.cwd is not None
        path = spec.cwd / scenario.fault.file
        old, new = scenario.fault.replace or ("", "")
        path.write_text(path.read_text().replace(new, old, 1))

    settings = LLMSettings(default_model="sonnet", escalation_model="opus")
    fixed = await run_scenario(scenario, FixingRunner(undo), settings, DBT.parent, tmp_path / "a")
    assert fixed.passed and fixed.ok, fixed.error
    assert [a["ok"] for a in fixed.attempts] == [True]

    never = await run_scenario(
        scenario, FixingRunner(lambda n, s: None), settings, DBT.parent, tmp_path / "b"
    )
    assert not never.passed and not never.ok and len(never.attempts) == 3
    table = summary([fixed, never])
    assert "| sql_typo | 1/2 |" in table and "**1/2**" in table
