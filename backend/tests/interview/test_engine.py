from pathlib import Path
from typing import Any

import pytest

from adp.beam import load
from adp.interview import (
    BeamUpdated,
    ChoicesPresented,
    Interviewer,
    InterviewEvent,
    Phase,
    PhaseChanged,
    TurnDone,
    new_state,
)
from adp.llm import LLMSettings, ScriptedRunner, TextDelta

FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "sales_by_store.yml"


async def collect(interviewer: Interviewer, state: Any, text: str) -> list[InterviewEvent]:
    return [e async for e in interviewer.send(state, text)]


@pytest.fixture
def expected() -> dict[str, Any]:
    return load(FIXTURE).model_dump(mode="json")


async def test_full_interview_with_scripted_agent(expected: dict[str, Any]) -> None:
    event = expected["events"][0]
    metric = expected["metrics"][0]
    runner = ScriptedRunner(
        [
            [
                ("set_overview", {"name": "sales_by_store", "title": "店舗別の売上"}),
                ("set_phase", {"phase": "framing"}),
                "「日別・店舗別の売上金額」という理解で合っていますか？",
                (
                    "present_choices",
                    {"question": "日別・店舗別でよいですか？", "options": ["はい", "いいえ"]},
                ),
            ],
            [
                ("set_phase", {"phase": "seven_ws"}),
                ("upsert_event", {"data": event}),
                ("upsert_metric", {"data": metric}),
                *[("add_gap", {"data": g}) for g in expected["gaps"]],
                ("set_phase", {"phase": "review"}),
                "内容をまとめました。問題なければ確定してください。",
            ],
        ]
    )
    interviewer = Interviewer(runner, LLMSettings())
    state = new_state("店舗ごとの売上を見たい")

    first = await collect(interviewer, state, state.beam.request)
    assert [e.phase for e in first if isinstance(e, PhaseChanged)] == []  # framing は初期値
    assert any(isinstance(e, ChoicesPresented) and e.options == ["はい", "いいえ"] for e in first)
    assert state.beam.name == "sales_by_store"
    done = first[-1]
    assert isinstance(done, TurnDone) and "理解で合っていますか" in done.message.text
    assert state.agent_session_id == "scripted"

    second = await collect(interviewer, state, "はい")
    assert state.choices == []  # 新しい発言で選択肢は消える
    updates = [e for e in second if isinstance(e, BeamUpdated)]
    assert updates and updates[-1].issues == []
    assert updates[-1].matrix.rows[0].event == "customer_orders_product"
    assert [e.phase for e in second if isinstance(e, PhaseChanged)] == [
        Phase.SEVEN_WS,
        Phase.REVIEW,
    ]
    assert runner.prompts == ["店舗ごとの売上を見たい", "はい"]
    assert [m.role for m in state.messages] == ["user", "assistant", "user", "assistant"]

    beam = interviewer.finalize(state)
    assert beam.finalized_at is not None and state.phase == Phase.FINALIZED
    with pytest.raises(ValueError, match="確定済み"):
        await collect(interviewer, state, "追加で")


async def test_invalid_tool_input_is_rejected_without_changing_state() -> None:
    runner = ScriptedRunner(
        [
            [
                ("set_overview", {"name": "Bad Name", "title": "x"}),
                ("upsert_event", {"data": {"name": "e"}}),
                ("set_phase", {"phase": "finalized"}),
                ("set_phase", {"phase": "nope"}),
            ]
        ]
    )
    state = new_state("売上")
    events = await collect(Interviewer(runner, LLMSettings()), state, "売上")
    assert not any(isinstance(e, BeamUpdated | PhaseChanged) for e in events)
    assert state.beam.name == "draft"
    assert all(r.get("is_error") for r in runner.tool_results)


async def test_successful_edit_reports_remaining_issues() -> None:
    runner = ScriptedRunner([[("set_overview", {"name": "sales", "title": "売上"}), "ok"]])
    state = new_state("売上")
    events = await collect(Interviewer(runner, LLMSettings()), state, "売上")
    text = runner.tool_results[0]["content"][0]["text"]
    assert "確定までに残っている課題" in text and "ビジネスイベントがありません" in text
    assert any(isinstance(e, TextDelta) for e in events)


async def test_finalize_requires_complete_beam() -> None:
    runner = ScriptedRunner([])
    interviewer = Interviewer(runner, LLMSettings())
    state = new_state("売上")
    with pytest.raises(ValueError, match="確定できません"):
        interviewer.finalize(state)
    assert state.phase == Phase.FRAMING


async def test_edit_after_review_clears_finalized_at(expected: dict[str, Any]) -> None:
    state = new_state("売上")
    state.beam = load(FIXTURE)
    state.beam.finalize()
    runner = ScriptedRunner([[("upsert_metric", {"data": expected["metrics"][0]})]])
    await collect(Interviewer(runner, LLMSettings()), state, "修正")
    assert state.beam.finalized_at is None


def test_tool_input_schemas_accept_valid_data(expected: dict[str, Any]) -> None:
    """SDK は input_schema で入力を検証する。$ref が解決できないと全ての呼び出しが失敗する。"""
    import jsonschema

    from adp.interview.tools import build_tools

    tools = {t.name: t for t in build_tools(new_state("x"))}
    samples = {
        "upsert_event": expected["events"][0],
        "upsert_metric": expected["metrics"][0],
        "add_gap": expected["gaps"][0],
    }
    for name, data in samples.items():
        schema = tools[name].input_schema
        assert isinstance(schema, dict)
        jsonschema.validate({"data": data}, schema)


async def test_question_from_present_choices_is_shown() -> None:
    runner = ScriptedRunner(
        [
            [
                ("present_choices", {"question": "売上は何を指しますか？", "options": ["a", "b"]}),
                "売上の種類を確認します。",
            ]
        ]
    )
    state = new_state("売上")
    events = await collect(Interviewer(runner, LLMSettings()), state, "売上")
    done = events[-1]
    assert isinstance(done, TurnDone)
    # 本文と質問文は別に持つ（画面で質問文が二重に出ないように。#36）
    assert done.message.text == "売上の種類を確認します。"
    assert done.message.question == "売上は何を指しますか？"
    assert done.message.choices == ["a", "b"]


def test_transcript_checks_detect_position_words_and_repeats() -> None:
    from adp.evals.interview import _transcript_checks
    from adp.interview import ChatMessage

    bad = [
        ChatMessage(role="assistant", text="上の選択肢から選んでください。", question="Q?"),
        ChatMessage(role="assistant", text="前置き。Q2?", question="Q2?"),
    ]
    results = {r.name: r.passed for r in _transcript_checks(bad)}
    assert results == {
        "no_choice_position_words": False,
        "question_not_repeated_in_text": False,
        "no_unexplained_jargon": True,
        "no_repeated_questions": True,
        "no_effort_question": True,
        "examples_shown": True,
    }
    good = [ChatMessage(role="assistant", text="支払方法は4種類です。", question="見たいですか？")]
    assert all(r.passed for r in _transcript_checks(good))


async def test_runner_exception_is_recorded_as_error_turn() -> None:
    class Broken:
        async def run(self, prompt, spec, resume=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("CLI crashed")
            yield  # pragma: no cover

    state = new_state("売上")
    events = await collect(Interviewer(Broken(), LLMSettings()), state, "売上")
    done = events[-1]
    assert isinstance(done, TurnDone) and done.result.is_error
    assert "CLI crashed" in (done.result.error or "")
    assert [m.role for m in state.messages] == ["user", "assistant"]


async def test_remove_gap_rejects_bad_index() -> None:
    runner = ScriptedRunner([[("remove_gap", {"index": -1})]])
    state = new_state("売上")
    await collect(Interviewer(runner, LLMSettings()), state, "売上")
    assert runner.tool_results[0].get("is_error")


def test_transcript_checks_tolerate_punctuation_and_other_ui_words() -> None:
    from adp.evals.interview import _transcript_checks
    from adp.interview import ChatMessage

    repeat = [
        ChatMessage(role="assistant", text="前置き。見たいですか? ", question="見たいですか？")
    ]
    assert not {r.name: r.passed for r in _transcript_checks(repeat)}[
        "question_not_repeated_in_text"
    ]
    ok = [ChatMessage(role="assistant", text="画面上のボタンの配置は自由です。", question="Q？")]
    assert all(r.passed for r in _transcript_checks(ok))


async def test_position_sentences_are_removed_and_recorded() -> None:
    """「上の質問にお答えください」は本文から取り除き、取り除いた文を記録する（#77）。"""
    runner = ScriptedRunner([["売上の種類は 3 つです。\n\n上の質問にお答えください。"]])
    state = new_state("売上")
    await collect(Interviewer(runner, LLMSettings()), state, "売上")
    message = state.messages[-1]
    assert message.text == "売上の種類は 3 つです。"
    assert message.removed == ["上の質問にお答えください。"]

    from adp.evals.interview import _transcript_checks

    checks = {r.name: r.passed for r in _transcript_checks(state.messages)}
    assert not checks["no_choice_position_words"]  # モデルの振る舞いとしては数える


async def test_closing_sentences_are_removed_when_a_question_is_shown() -> None:
    """質問を出した発言の締めの一言は取り除く。本文が空になってもよい（#158）。"""
    runner = ScriptedRunner(
        [
            [
                ("present_choices", {"question": "優先度はどのくらいですか？", "options": ["a"]}),
                "優先度について、ご回答をお待ちしています。",
            ],
            ["ご回答をお待ちしています。"],
        ]
    )
    state = new_state("売上")
    await collect(Interviewer(runner, LLMSettings()), state, "売上")
    message = state.messages[-1]
    assert message.text == ""
    assert message.removed == ["優先度について、ご回答をお待ちしています。"]
    # 質問が無い発言では残す
    await collect(Interviewer(runner, LLMSettings()), state, "高い")
    assert state.messages[-1].text == "ご回答をお待ちしています。"
