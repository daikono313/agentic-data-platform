"""インタビューの進行。1 回のユーザー発言ごとに 1 ターン、エージェントを実行する。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import SdkMcpTool

from adp.beam import BeamModel, EventMatrix, build_event_matrix
from adp.interview import tools as beam_tools
from adp.interview.prompts import prompt_version, system_prompt
from adp.interview.state import ChatMessage, InterviewState, Phase
from adp.interview.wording import clean_reply
from adp.llm import (
    AgentRunner,
    AgentSpec,
    LLMSettings,
    TextDelta,
    ToolCall,
    TurnResult,
    join_chunks,
)


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


logger = logging.getLogger(__name__)


class Interviewer:
    def __init__(
        self,
        runner: AgentRunner,
        settings: LLMSettings,
        extra_tools: dict[str, list[SdkMcpTool[Any]]] | None = None,
        max_turns: int = 30,
        design_issues: Callable[[BeamModel], list[str]] | None = None,
    ) -> None:
        self.runner = runner
        self.settings = settings
        self.extra_tools = extra_tools or {}
        self.max_turns = max_turns
        self.design_issues = design_issues
        """確定前に、設計で止まる課題（結合の経路が無いなど）を見つける（#46）。"""
        self._locks: dict[int, asyncio.Lock] = {}

    def issues(self, beam: BeamModel) -> list[str]:
        """確定までに残っている課題。BEAM の条件がそろってから、設計の課題も確かめる。"""
        issues = beam.finalize_issues()
        if issues or self.design_issues is None:
            return issues
        try:
            return [f"設計: {i}" for i in self.design_issues(beam)]
        except Exception:  # 設計の確認自体の失敗で確定を止めない（生成時にも確かめる）
            logger.exception("設計の確認に失敗しました（確定は止めません）")
            return []

    def _spec(self, state: InterviewState, shown: Callable[[], tuple[str, str]]) -> AgentSpec:
        tools = beam_tools.build_tools(state, issues=self.issues, shown=shown)
        return AgentSpec(
            purpose="interview",
            system_prompt=system_prompt(),
            model=self.settings.default_model,
            tools={beam_tools.SERVER_NAME: tools} | self.extra_tools,
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
        previous = next((m for m in reversed(state.messages) if m.role == "assistant"), None)
        state.messages.append(ChatMessage(role="user", text=text))
        state.choices = []
        state.question = None
        snapshot = _Snapshot.of(state, self.issues)

        # 本文のかたまり（tool 呼び出しで区切る。#161）
        reply: list[str] = [""]
        # ユーザーに見えている直近の文（アシスタントの直前の発言と今の発言の本文、ユーザーの発言。
        # 確認の質問で中身が見えているかに使う。#163・#165）
        seen = "\n".join([previous.text, previous.question or ""] if previous else [])

        def shown() -> tuple[str, str]:
            return seen + "\n" + join_chunks(reply), text

        result: TurnResult | None = None
        try:
            async for event in self.runner.run(
                text, self._spec(state, shown), resume=state.agent_session_id
            ):
                for change in snapshot.diff(state):
                    yield change
                if isinstance(event, TextDelta):
                    # tool 呼び出しの後の本文は空行で区切る
                    if event.text and not reply[-1] and any(c.strip() for c in reply):
                        yield TextDelta("\n\n")
                    reply[-1] += event.text
                    yield event
                elif isinstance(event, ToolCall):
                    if reply[-1]:
                        reply.append("")
                    yield event
                else:
                    result = event
        except asyncio.CancelledError:
            # クライアントの切断など。途中までの応答を残してから中断する
            partial, removed = clean_reply(join_chunks(reply))
            state.messages.append(
                ChatMessage(
                    role="assistant",
                    text=(partial + "\n（中断されました）").strip(),
                    removed=removed,
                )
            )
            raise
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
        text = join_chunks(reply)
        if result.is_error and not text:
            text = "（応答を作れませんでした。もう一度送ってください）"
        # 画面の位置を指す文（「上の質問にお答えください」など。#77）、英語の作業メモ（#161）と、
        # 質問を出したときの締めの一言（「回答をお待ちしています」など。#158）は取り除く
        text, removed = clean_reply(text, question=state.question)
        # 質問文は本文に混ぜずに別に持つ（画面ではボタンの上に 1 回だけ出す。#36）
        message = ChatMessage(
            role="assistant",
            text=text,
            question=state.question,
            choices=list(state.choices),
            phase=state.phase.value,
            prompt_version=prompt_version(),
            model=result.model,
            removed=removed,
        )
        state.messages.append(message)
        yield TurnDone(result=result, message=message)

    def is_busy(self, state: InterviewState) -> bool:
        lock = self._locks.get(id(state))
        return lock is not None and lock.locked()

    def forget(self, state: InterviewState) -> None:
        """メモリから外した状態の排他のロックを捨てる（応答中なら残す）。"""
        lock = self._locks.get(id(state))
        if lock is not None and not lock.locked():
            del self._locks[id(state)]

    def reopen(self, state: InterviewState) -> None:
        """確定を取り消して確認の段階に戻す（修正して確定し直すため）。"""
        if state.phase != Phase.FINALIZED:
            raise ValueError("確定していないセッションです")
        beam = state.beam.model_copy(deep=True)
        beam.finalized_at = None
        state.beam = beam
        state.phase = Phase.REVIEW

    def finalize(self, state: InterviewState) -> BeamModel:
        """ユーザーの確定操作。確定条件を満たしていなければ ValueError。"""
        issues = self.issues(state.beam)
        if issues:
            raise ValueError("確定できません: " + " / ".join(issues))
        beam = state.beam.model_copy(deep=True)
        beam.finalize()
        state.beam = beam
        state.phase = Phase.FINALIZED
        state.choices = []
        return beam


def snapshot_event(
    state: InterviewState, issues: Callable[[BeamModel], list[str]] | None = None
) -> BeamUpdated:
    return BeamUpdated(
        beam=state.beam,
        matrix=build_event_matrix(state.beam),
        issues=(issues or BeamModel.finalize_issues)(state.beam),
    )


@dataclass
class _Snapshot:
    beam_json: str
    phase: Phase
    choices: list[str]
    question: str | None
    issues: Callable[[BeamModel], list[str]] | None = None

    @classmethod
    def of(
        cls, state: InterviewState, issues: Callable[[BeamModel], list[str]] | None = None
    ) -> _Snapshot:
        return cls(
            state.beam.model_dump_json(), state.phase, list(state.choices), state.question, issues
        )

    def diff(self, state: InterviewState) -> list[InterviewEvent]:
        changes: list[InterviewEvent] = []
        beam_json = state.beam.model_dump_json()
        if beam_json != self.beam_json:
            self.beam_json = beam_json
            changes.append(snapshot_event(state, self.issues))
        if state.phase != self.phase:
            self.phase = state.phase
            changes.append(PhaseChanged(state.phase))
        if state.choices != self.choices or state.question != self.question:
            self.choices = list(state.choices)
            self.question = state.question
            changes.append(ChoicesPresented(list(state.choices), state.question))
        return changes
