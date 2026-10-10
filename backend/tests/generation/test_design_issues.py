"""確定前に設計の課題を見つける（#46）。

BEAM の確定条件を満たしていても、ソースの結合の経路が無いなど設計で止まるものは、
インタビュー中の「確定までに残っている課題」に出し、確定させない。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from adp.beam import BeamModel, load
from adp.catalog.catalog import Catalog
from adp.design.check import design_issues
from adp.interview import Interviewer, new_state
from adp.llm import LLMSettings, ScriptedRunner
from tests.generation.test_dbt_codegen import FIXTURES, _warehouse_copy

RECORD = Path(__file__).parents[1] / "fixtures" / "evals" / "sales_by_store.json"


def _turns() -> list[list[Any]]:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    return [
        [
            (c["name"].removeprefix("mcp__beam__"), c["input"])
            for c in turn
            if c["name"].startswith("mcp__beam__")
        ]
        for turn in record["tool_calls"]
    ]


async def _interview(design: Any) -> tuple[Interviewer, Any, ScriptedRunner]:
    runner = ScriptedRunner(_turns())
    interviewer = Interviewer(runner, LLMSettings(), design_issues=design)
    state = new_state("店舗ごとの売上を見たい")
    for _ in _turns():
        async for _event in interviewer.send(state, "回答"):
            pass
    return interviewer, state, runner


async def test_design_issues_block_finalize_and_are_told_to_the_agent() -> None:
    def blockers(beam: BeamModel) -> list[str]:
        return ["raw.order_items から raw.returns を結合する経路が見つかりません"]

    interviewer, state, runner = await _interview(blockers)
    assert state.beam.finalize_issues() == []  # BEAM の条件はそろっている
    issues = interviewer.issues(state.beam)
    assert issues == ["設計: raw.order_items から raw.returns を結合する経路が見つかりません"]
    # エージェントへの tool の応答にも出る（直してから確定に進めるように）
    texts = [r["content"][0]["text"] for r in runner.tool_results]
    assert any("設計: raw.order_items から raw.returns" in t for t in texts)
    with pytest.raises(ValueError, match="設計"):
        interviewer.finalize(state)


async def test_without_design_issues_finalize_works() -> None:
    interviewer, state, _ = await _interview(lambda beam: [])
    assert interviewer.issues(state.beam) == []
    assert interviewer.finalize(state).finalized_at is not None


async def test_failing_design_check_does_not_block() -> None:
    def broken(beam: BeamModel) -> list[str]:
        raise RuntimeError("catalog unavailable")

    interviewer, state, _ = await _interview(broken)
    assert interviewer.issues(state.beam) == []


def test_design_issues_with_the_real_catalog(tmp_path: Path) -> None:
    wh = _warehouse_copy(tmp_path)
    check = design_issues(Catalog(wh / "data" / "dev.duckdb"))
    good = load(FIXTURES / "beam" / "sales_by_store.yml")
    assert check(good) == []
    # 注文のイベントに、返品のテーブルの属性を入れてしまう（結合の経路が無い）
    data = good.model_dump(mode="json")
    event = data["events"][0]
    event["degenerate_dimensions"].append(
        {
            "name": "return_reason",
            "label": "返品理由",
            "source": {"table": "raw.returns", "column": "return_reason"},
        }
    )
    bad = BeamModel.model_validate(data)
    assert any("結合" in i or "経路" in i for i in check(bad)), check(bad)
