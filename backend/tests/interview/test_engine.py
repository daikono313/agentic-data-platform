import asyncio
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
from adp.llm import LLMSettings, ScriptedRunner, TextDelta, ToolCall

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


async def test_text_across_tool_calls_is_separated() -> None:
    # tool 呼び出しをまたいだ本文は空行で区切る。書き直した文は消さない（#161）
    runner = ScriptedRunner(
        [
            [
                "- 数量: 54 個",
                ("set_overview", {"name": "sales", "title": "売上"}),
                "次のように整理しました。",
                ("set_overview", {"name": "sales", "title": "売上"}),
                "次のように整理しました。続けます。",
            ]
        ]
    )
    state = new_state("売上")
    events = await collect(Interviewer(runner, LLMSettings()), state, "売上")
    streamed = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert (
        streamed
        == "- 数量: 54 個\n\n次のように整理しました。\n\n次のように整理しました。続けます。"
    )
    assert state.messages[-1].text == streamed


class _CancelledRunner:
    """本文と tool 呼び出しの途中で中断される偽エージェント。"""

    async def run(self, prompt: str, spec: Any, resume: str | None = None) -> Any:
        yield TextDelta("前半です。")
        yield ToolCall("set_phase", {})
        yield TextDelta("後半です。")
        raise asyncio.CancelledError


async def test_cancelled_reply_keeps_chunks_separated() -> None:
    state = new_state("売上")
    with pytest.raises(asyncio.CancelledError):
        await collect(Interviewer(_CancelledRunner(), LLMSettings()), state, "売上")  # type: ignore[arg-type]
    assert state.messages[-1].text == "前半です。\n\n後半です。\n（中断されました）"


async def test_tool_call_first_does_not_stream_separator() -> None:
    runner = ScriptedRunner([[("set_overview", {"name": "sales", "title": "売上"}), "本文です。"]])
    events = await collect(Interviewer(runner, LLMSettings()), new_state("売上"), "売上")
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["本文です。"]


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


async def test_example_confirmation_needs_values_the_user_can_see() -> None:
    """値を書かずに具体例を確かめる質問は出さず、値を書くよう tool のエラーで返す（#163）。"""
    confirm = {"question": "この 2 件を具体例として記録してよいですか？", "options": ["よい", "別"]}
    runner = ScriptedRunner(
        [
            [("present_choices", confirm)],
            [
                "- 2024-11-24 に 福岡店で 1 個\n- 2025-02-12 に 大阪店で 2 個",
                ("present_choices", confirm),
            ],
            [("present_choices", confirm)],
            [("present_choices", {"question": "質問?", "options": ["a", "b"]})],
            [("present_choices", confirm)],
        ]
    )
    interviewer = Interviewer(runner, LLMSettings())
    state = new_state("売上")
    first = await collect(interviewer, state, "売上")
    assert runner.tool_results[0].get("is_error") is True
    # 具体例の値も question に書かせ、具体例の形の例を示す（#170）
    error = runner.tool_results[0]["content"][0]["text"]
    assert "question の中に" in error and "具体例として記録してよいですか" in error
    done = first[-1]
    assert isinstance(done, TurnDone) and done.message.question is None
    assert not any(isinstance(e, ChoicesPresented) for e in first)
    # 本文で値を示していれば出せる
    second = await collect(interviewer, state, "はい")
    assert "is_error" not in runner.tool_results[1]
    done = second[-1]
    assert isinstance(done, TurnDone) and done.message.question == confirm["question"]
    # 直前の発言で値を示していても出せる
    await collect(interviewer, state, "もう一度")
    assert "is_error" not in runner.tool_results[2]
    # 言い換えた問いを書かずに確かめる質問も、同じく出さない（#165）
    reframe = {"question": "この言い換えで合っていますか？", "options": ["はい", "いいえ"]}
    runner2 = ScriptedRunner(
        [
            [("present_choices", reframe)],
            ["「月ごとの売上金額はいくらか？」と言い換えました。", ("present_choices", reframe)],
        ]
    )
    other = Interviewer(runner2, LLMSettings())
    state2 = new_state("売上")
    await collect(other, state2, "売上")
    error = runner2.tool_results[0]["content"][0]["text"]
    # 言い換えた問いは本文ではなく question に入れさせる（本文から「上の言い換え」と指さない。#168）
    assert "言い換えた問い" in error and "question の中に" in error
    await collect(other, state2, "はい")
    assert "is_error" not in runner2.tool_results[1]
    # question の中に言い換えた問いがあれば、本文が無くても出せる
    inline = {
        "question": "「月ごとの売上金額はいくらか」という言い換えで合っていますか？",
        "options": ["はい", "いいえ"],
    }
    runner3 = ScriptedRunner([[("present_choices", inline)]])
    await collect(Interviewer(runner3, LLMSettings()), new_state("売上"), "売上")
    assert "is_error" not in runner3.tool_results[0]
    # ユーザーが自分で書いた例を確かめるときも出せる
    await collect(interviewer, state, "ほか")
    await collect(interviewer, state, "例: 2024-05-01 に 3 個入荷")
    assert "is_error" not in runner.tool_results[4]


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
        "reframe_shown": True,
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
