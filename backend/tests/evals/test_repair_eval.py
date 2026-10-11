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
from adp.generation.semantic import MetricQuery
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


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, list[MetricQuery]]]:
    """生成した直後のプロジェクト（検証が通る状態）と、BEAM ごとの元の要求の mf query。"""
    from adp.generation.repair import verify

    warehouse = prepare_warehouse(tmp_path_factory.mktemp("built"))
    info = ProjectInfo(warehouse, Catalog(warehouse / "data" / "dev.duckdb"))
    queries: dict[str, list[MetricQuery]] = {}
    for beam_path in {s.beam for s in SCENARIOS}:
        beam = load(REPO_ROOT / beam_path)
        gen = generate_models(DesignBuilder(CatalogSchema(info.catalog)).build(beam), info, beam)
        info.write(gen.files)
        queries[beam_path] = gen.queries
    assert verify(warehouse, DBT.parent, [q for qs in queries.values() for q in qs]).ok
    return warehouse, queries


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_each_fault_breaks_verification(
    scenario: RepairScenario,
    built: tuple[Path, dict[str, list[MetricQuery]]],
    generated: dict[str, str],
) -> None:
    """失敗を入れると検証が失敗し、元に戻すと（テストの名前も含めて）通る。

    直せないシナリオ（dbt で読めない・正しく直すとテストの名前が変わる）を見つける。
    """
    from adp.generation.repair import test_inventory, verify, weakened_tests

    warehouse, queries = built
    baseline = test_inventory(warehouse, DBT.parent)
    assert baseline is not None
    path = warehouse / scenario.fault.file
    try:
        scenario.fault.apply(warehouse)
        assert not verify(warehouse, DBT.parent, queries[scenario.beam]).ok
    finally:
        path.write_text(generated[scenario.fault.file], encoding="utf-8")
    assert weakened_tests(baseline, test_inventory(warehouse, DBT.parent)) == []


async def test_harness_counts_an_honest_fix(tmp_path: Path) -> None:
    """失敗を入れ、元に戻すエージェントなら「期待どおり」「元に戻した」になる。"""
    scenario = next(s for s in SCENARIOS if s.name == "wrong_ref")  # dbt で読めなくなる失敗

    def undo(n: int, spec: AgentSpec) -> None:
        assert spec.cwd is not None
        path = spec.cwd / scenario.fault.file
        old, new = scenario.fault.replace or ("", "")
        path.write_text(path.read_text().replace(new, old, 1))

    settings = LLMSettings(default_model="sonnet", escalation_model="opus")
    fixed = await run_scenario(scenario, FixingRunner(undo), settings, DBT.parent, tmp_path)
    assert fixed.passed and fixed.ok and fixed.restored, (fixed.error, fixed.final)
    assert fixed.changed == [] and [a["ok"] for a in fixed.attempts] == [True]


def test_summary_table() -> None:
    from adp.evals.repair import RepairEvalResult

    rs = [
        RepairEvalResult("a", passed=True, ok=True, expect="fixed", restored=True, attempts=[{}]),
        RepairEvalResult("a", passed=False, ok=False, expect="fixed", attempts=[{}, {}, {}]),
    ]
    table = summary(rs)
    assert "| a | 1/2 | 1/2 | 2.0 |" in table and "**1/2**" in table


def test_scenario_name_must_match_file(tmp_path: Path) -> None:
    path = tmp_path / "x.yml"
    path.write_text("name: y\nbeam: b.yml\nfault: {file: a.sql, replace: [a, b]}\n")
    with pytest.raises(ValueError, match="ファイル名"):
        RepairScenario.load(path)
