"""エージェント実行の抽象化。実装は Claude Agent SDK（本番）とスクリプト（テスト）の2つ。"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SdkMcpTool,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
)

from adp.llm.settings import REPO_ROOT, LLMSettings


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
    purpose: str = "agent"
    """使用量の記録に書く用途（interview / repair / eval-user など）。"""
    thinking: bool = True
    """False にすると拡張思考を無効にする（対話の応答を速くし、内容を思考に隠させない）。"""
    allowed_builtin: list[str] = field(default_factory=list)
    """確認なしで許可する組み込み tool のパターン（例: `Bash(dbt build:*)`）。"""
    pre_tool_use: PreToolUseHook | None = None
    """組み込み tool の実行前に呼ばれ、拒否理由（文字列）を返すと実行を止める。"""

    @property
    def allowed_tools(self) -> list[str]:
        names = [f"mcp__{srv}__{t.name}" for srv, ts in self.tools.items() for t in ts]
        return names + (self.allowed_builtin or self.builtin_tools)


@dataclass
class TextDelta:
    text: str


def join_chunks(chunks: list[str]) -> str:
    """tool 呼び出しで分かれた本文（TextDelta）のかたまりを空行でつなぐ（区切り無しでつながらない
    ように。#161）。かたまりの頭のインデントは残す。書き直した二重のかたまりはここでは消さない
    （一字一句同じ段落だけを、画面に出す前に `adp.interview.wording.clean_reply` で取り除く）。"""
    return "\n\n".join(c.strip("\n").rstrip() for c in chunks if c.strip())


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
    """キャッシュの読み書きを含む入力トークンの合計。"""
    output_tokens: int | None = None
    error: str | None = None
    cost_usd: float | None = None
    """SDK が報告する概算コスト（Claude Code のログイン認証では実際の請求ではない）。"""


AgentEvent = TextDelta | ToolCall | TurnResult

PreToolUseHook = Callable[[str, dict[str, Any]], str | None]
"""(tool 名, 入力) → 拒否理由。None なら許可。"""


class AgentRunner(Protocol):
    def run(
        self, prompt: str, spec: AgentSpec, resume: str | None = None
    ) -> AsyncIterator[AgentEvent]: ...


USAGE_LOG_MAX_BYTES = 10 * 1024 * 1024
"""使用量ログがこれを超えたら .1 に退避して新しく始める。"""


def _input_tokens(usage: dict[str, Any]) -> int | None:
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    values = [v for k in keys if isinstance(v := usage.get(k), int)]
    return sum(values) if values else None


class ClaudeAgentRunner:
    """Claude Agent SDK（Claude Code CLI）で実行する。"""

    def __init__(self, settings: LLMSettings, usage_log: Path | None = None) -> None:
        self.settings = settings
        self.usage_log = (
            usage_log if usage_log is not None else REPO_ROOT / ".adp" / "logs" / "llm.jsonl"
        )

    def _record(self, result: TurnResult, purpose: str) -> None:
        """使用量を JSON Lines で記録する（#16）。プロンプトや応答の本文は記録しない。"""
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "purpose": purpose,
            "auth": self.settings.auth_mode,
            "model": result.model,
            "session_id": result.session_id,
            "is_error": result.is_error,
            "duration_s": round(result.duration_s, 2),
            "first_token_s": (
                round(result.first_token_s, 2) if result.first_token_s is not None else None
            ),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
        }
        try:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            if self.usage_log.exists() and self.usage_log.stat().st_size > USAGE_LOG_MAX_BYTES:
                self.usage_log.replace(self.usage_log.with_suffix(".jsonl.1"))
            with self.usage_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 記録の失敗で処理を止めない

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
            # 許可リストに無い操作は確認を求めず拒否する（非対話のため）
            permission_mode="dontAsk",
            hooks=_hooks(spec.pre_tool_use) if spec.pre_tool_use else None,
        )

    async def run(
        self, prompt: str, spec: AgentSpec, resume: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        start = time.monotonic()
        recorded = False
        try:
            async for event in self._stream(prompt, spec, resume, start):
                if isinstance(event, TurnResult):
                    recorded = True
                yield event
        finally:
            if not recorded:  # 中断・異常終了でも、使った時間は記録する
                self._record(
                    TurnResult(
                        session_id=resume,
                        model=spec.model,
                        is_error=True,
                        duration_s=time.monotonic() - start,
                        first_token_s=None,
                        error="中断または異常終了",
                    ),
                    spec.purpose,
                )

    async def _stream(
        self, prompt: str, spec: AgentSpec, resume: str | None, start: float
    ) -> AsyncIterator[AgentEvent]:
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
                result = TurnResult(
                    session_id=message.session_id,
                    model=spec.model,
                    is_error=message.is_error,
                    duration_s=time.monotonic() - start,
                    first_token_s=first,
                    input_tokens=_input_tokens(usage),
                    output_tokens=usage.get("output_tokens"),
                    error=_error_text(message) if message.is_error else None,
                    cost_usd=message.total_cost_usd,
                )
                self._record(result, spec.purpose)
                yield result


def _error_text(message: ResultMessage) -> str:
    """エラー終了の理由。max_turns などでは result が空で errors / subtype に入る。"""
    errors = getattr(message, "errors", None) or []
    detail = message.result or "; ".join(str(e) for e in errors)
    return f"{message.subtype}: {detail}" if detail else str(message.subtype)


def _hooks(check: PreToolUseHook) -> dict[Any, list[HookMatcher]]:
    async def pre_tool_use(data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        reason = check(str(data.get("tool_name", "")), dict(data.get("tool_input", {})))
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]}  # type: ignore[list-item]


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
