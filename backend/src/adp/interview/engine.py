"""インタビューの進行。1 回のユーザー発言ごとに 1 ターン、エージェントを実行する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import SdkMcpTool

from adp.beam import BeamModel, EventMatrix, build_event_matrix
from adp.interview import tools as beam_tools
from adp.interview.prompts import system_prompt
from adp.interview.state import ChatMessage, InterviewState, Phase
from adp.llm import AgentRunner, AgentSpec, LLMSettings, TextDelta, ToolCall, TurnResult


@dataclass
class BeamUpdated:
    beam: BeamModel
    matrix: EventMatrix
    issues: list[str]


@dataclass
class PhaseChanged:
    phase: Phase


@dataclass
class ChoicesPresented:
    options: list[str]
    question: str | None = None


@dataclass
class TurnDone:
    result: TurnResult
    message: ChatMessage


InterviewEvent = TextDelta | ToolCall | BeamUpdated | PhaseChanged | ChoicesPresented | TurnDone


def new_state(request: str) -> InterviewState:
    return InterviewState(beam=BeamModel(name="draft", title="下書き", request=request))


class Interviewer:
    def __init__(
        self,
        runner: AgentRunner,
        settings: LLMSettings,
        extra_tools: dict[str, list[SdkMcpTool[Any]]] | None = None,
        max_turns: int = 30,
    ) -> None:
        self.runner = runner
        self.settings = settings
        self.extra_tools = extra_tools or {}
        self.max_turns = max_turns
        self._locks: dict[int, asyncio.Lock] = {}

    def _spec(self, state: InterviewState) -> AgentSpec:
        return AgentSpec(
            system_prompt=system_prompt(),
            model=self.settings.default_model,
            tools={beam_tools.SERVER_NAME: beam_tools.build_tools(state)} | self.extra_tools,
            max_turns=self.max_turns,
            thinking=False,
        )

    async def send(self, state: InterviewState, text: str) -> AsyncIterator[InterviewEvent]:
        """ユーザーの発言を渡し、エージェントの応答と状態の変化をイベントとして返す。

        最初の要求文も send で渡す。tool による状態の変化は、エージェントのイベントの合間に
        差分を見て BeamUpdated / PhaseChanged / ChoicesPresented として返す。
        """
        if state.phase == Phase.FINALIZED:
            raise ValueError("確定済みのセッションです")
        lock = self._locks.setdefault(id(state), asyncio.Lock())
        if lock.locked():
            raise RuntimeError("前の応答を処理中です")
        async with lock:
            async for event in self._send(state, text):
                yield event

    async def _send(self, state: InterviewState, text: str) -> AsyncIterator[InterviewEvent]:
        state.messages.append(ChatMessage(role="user", text=text))
        state.choices = []
        state.question = None
        snapshot = _Snapshot.of(state)

        reply: list[str] = []
        result: TurnResult | None = None
        try:
            async for event in self.runner.run(
                text, self._spec(state), resume=state.agent_session_id
            ):
                for change in snapshot.diff(state):
                    yield change
                if isinstance(event, TextDelta):
                    reply.append(event.text)
                    yield event
                elif isinstance(event, ToolCall):
                    yield event
                else:
                    result = event
        except Exception as e:  # CLI の異常終了など。会話は続けられるように記録して返す
            result = TurnResult(
                session_id=None,
                model=self.settings.default_model,
                is_error=True,
                duration_s=0.0,
                first_token_s=None,
                error=f"エージェントの実行に失敗しました: {e}",
            )
        for change in snapshot.diff(state):
            yield change

        if result is None:
            result = TurnResult(
                session_id=None,
                model=self.settings.default_model,
                is_error=True,
                duration_s=0.0,
                first_token_s=None,
                error="エージェントが応答を返しませんでした",
            )
        if result.session_id:
            state.agent_session_id = result.session_id
        text = "".join(reply).strip()
        # 選択肢つきの質問は、本文の後ろに質問文を付けて表示する（本文では繰り返さない約束）
        if state.question and state.question not in text:
            text = f"{text}\n\n{state.question}".strip()
        if result.is_error and not text:
            text = "（応答を作れませんでした。もう一度送ってください）"
        message = ChatMessage(role="assistant", text=text)
        state.messages.append(message)
        yield TurnDone(result=result, message=message)

    def finalize(self, state: InterviewState) -> BeamModel:
        """ユーザーの確定操作。確定条件を満たしていなければ ValueError。"""
        beam = state.beam.model_copy(deep=True)
        beam.finalize()
        state.beam = beam
        state.phase = Phase.FINALIZED
        state.choices = []
        return beam


def snapshot_event(state: InterviewState) -> BeamUpdated:
    return BeamUpdated(
        beam=state.beam,
        matrix=build_event_matrix(state.beam),
        issues=state.beam.finalize_issues(),
    )


@dataclass
class _Snapshot:
    beam_json: str
    phase: Phase
    choices: list[str]

    @classmethod
    def of(cls, state: InterviewState) -> _Snapshot:
        return cls(state.beam.model_dump_json(), state.phase, list(state.choices))

    def diff(self, state: InterviewState) -> list[InterviewEvent]:
        changes: list[InterviewEvent] = []
        beam_json = state.beam.model_dump_json()
        if beam_json != self.beam_json:
            self.beam_json = beam_json
            changes.append(snapshot_event(state))
        if state.phase != self.phase:
            self.phase = state.phase
            changes.append(PhaseChanged(state.phase))
        if state.choices != self.choices:
            self.choices = list(state.choices)
            changes.append(ChoicesPresented(list(state.choices), state.question))
        return changes
