"""実際の LLM での eval（make eval）の tool 呼び出しを再生し、同じ成果物になることを確認する。

CI では LLM を呼ばない代わりに、記録した呼び出しで BEAM の tool・検証・確定が
今のコードでも同じように通ることを確かめる（certificate C5）。
fixture は `make eval` の結果（.adp/evals/<日時>/<シナリオ>.json）から作る。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from adp.evals.interview import _check, is_transcript_check
from adp.interview import Interviewer, new_state
from adp.llm import LLMSettings, ScriptedRunner

FIXTURES = sorted((Path(__file__).parents[1] / "fixtures" / "evals").glob("*.json"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
async def test_replay_recorded_eval(path: Path) -> None:
    import yaml

    from adp.llm.settings import REPO_ROOT

    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    beam_calls = [
        [
            (c["name"].removeprefix("mcp__beam__"), c["input"])
            for c in turn
            if c["name"].startswith("mcp__beam__")
        ]
        for turn in record["tool_calls"]
    ]
    runner = ScriptedRunner(beam_calls)  # type: ignore[arg-type]
    interviewer = Interviewer(runner, LLMSettings())
    state = new_state(record["beam"]["request"])
    for _ in beam_calls:
        async for _event in interviewer.send(state, "（再生）"):
            pass
    # 記録した成果物と同じものが、今のコードでも tool の呼び出しから組み立てられる
    assert state.beam.model_dump(mode="json") == record["beam"] | {"finalized_at": None}
    interviewer.finalize(state)

    scenario = yaml.safe_load(
        (REPO_ROOT / "evals" / "interview" / f"{record['scenario']}.yml").read_text(
            encoding="utf-8"
        )
    )
    # 再生では会話の文が無いので、BEAM 成果物のチェックだけを行う
    results = [_check(state.beam, c) for c in scenario["checks"] if not is_transcript_check(c)]
    assert all(r.passed for r in results), [(r.name, r.detail) for r in results if not r.passed]
