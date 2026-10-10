"""エージェント実行の抽象化。実装は Claude Agent SDK（本番）とスクリプト（テスト）の2つ。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SdkMcpTool,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
)

from adp.llm.settings import LLMSettings


@dataclass
class AgentSpec:
    """1回のエージェント実行の設定。"""

    system_prompt: str
    model: str
    tools: dict[str, list[SdkMcpTool[Any]]] = field(default_factory=dict)
    """MCP サーバー名 → tool。tool 名は `mcp__<server>__<tool>` になる。"""
    builtin_tools: list[str] = field(default_factory=list)
    """Claude Code の組み込み tool（Read / Edit / Bash など）。既定は無し。"""
    max_turns: int = 20
    cwd: Path | None = None
    thinking: bool = True
    """False にすると拡張思考を無効にする（対話の応答を速くし、内容を思考に隠させない）。"""

    @property
    def allowed_tools(self) -> list[str]:
        names = [f"mcp__{srv}__{t.name}" for srv, ts in self.tools.items() for t in ts]
        return names + self.builtin_tools


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]


@dataclass
class TurnResult:
    session_id: str | None
    model: str
    is_error: bool
    duration_s: float
    first_token_s: float | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


AgentEvent = TextDelta | ToolCall | TurnResult


class AgentRunner(Protocol):
    def run(
        self, prompt: str, spec: AgentSpec, resume: str | None = None
    ) -> AsyncIterator[AgentEvent]: ...


class ClaudeAgentRunner:
    """Claude Agent SDK（Claude Code CLI）で実行する。"""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    def _options(self, spec: AgentSpec, resume: str | None) -> ClaudeAgentOptions:
        servers: dict[str, Any] = {
            name: create_sdk_mcp_server(name=name, tools=tools)
            for name, tools in spec.tools.items()
        }
        return ClaudeAgentOptions(
            model=spec.model,
            system_prompt=spec.system_prompt,
            mcp_servers=servers,
            allowed_tools=spec.allowed_tools,
            tools=spec.builtin_tools,
            # ユーザーやリポジトリの設定・CLAUDE.md・claude.ai コネクタを読み込まない
            setting_sources=[],
            extra_args={"strict-mcp-config": None},
            max_turns=spec.max_turns,
            cwd=str(spec.cwd) if spec.cwd else None,
            resume=resume,
            include_partial_messages=True,
            env=self.settings.subprocess_env(),
            thinking=None if spec.thinking else {"type": "disabled"},
        )

    async def run(
        self, prompt: str, spec: AgentSpec, resume: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        start = time.monotonic()
        first: float | None = None
        streamed = False
        async for message in query(prompt=prompt, options=self._options(spec, resume)):
            if isinstance(message, StreamEvent):
                event = message.event
                delta = event.get("delta", {}) if event.get("type") == "content_block_delta" else {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    first = first if first is not None else time.monotonic() - start
                    streamed = True
                    yield TextDelta(delta["text"])
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and not streamed:
                        first = first if first is not None else time.monotonic() - start
                        yield TextDelta(block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield ToolCall(block.name, dict(block.input))
                streamed = False
            elif isinstance(message, ResultMessage):
                usage = message.usage or {}
                yield TurnResult(
                    session_id=message.session_id,
                    model=spec.model,
                    is_error=message.is_error,
                    duration_s=time.monotonic() - start,
                    first_token_s=first,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    error=_error_text(message) if message.is_error else None,
                )


def _error_text(message: ResultMessage) -> str:
    """エラー終了の理由。max_turns などでは result が空で errors / subtype に入る。"""
    errors = getattr(message, "errors", None) or []
    detail = message.result or "; ".join(str(e) for e in errors)
    return f"{message.subtype}: {detail}" if detail else str(message.subtype)


ScriptStep = str | tuple[str, dict[str, Any]]
"""テキスト、または (tool 名, 引数)。tool 名は MCP の接頭辞なし。"""

ScriptFn = Callable[[str, int], list[ScriptStep]]


class ScriptedRunner:
    """テスト用。台本どおりに tool を実際に呼び、テキストを返す。LLM は呼ばない。"""

    def __init__(self, script: ScriptFn | list[list[ScriptStep]]) -> None:
        self._script = script
        self.turn = 0
        self.prompts: list[str] = []
        self.tool_results: list[dict[str, Any]] = []

    def _steps(self, prompt: str) -> list[ScriptStep]:
        if callable(self._script):
            return self._script(prompt, self.turn)
        return self._script[self.turn] if self.turn < len(self._script) else []

    async def run(
        self, prompt: str, spec: AgentSpec, resume: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        self.prompts.append(prompt)
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            t.name: t.handler for ts in spec.tools.values() for t in ts
        }
        for step in self._steps(prompt):
            if isinstance(step, str):
                yield TextDelta(step)
            else:
                name, args = step
                yield ToolCall(name, args)
                self.tool_results.append(await handlers[name](args))
        self.turn += 1
        yield TurnResult(
            session_id=resume or "scripted",
            model=spec.model,
            is_error=False,
            duration_s=0.0,
            first_token_s=0.0,
        )
