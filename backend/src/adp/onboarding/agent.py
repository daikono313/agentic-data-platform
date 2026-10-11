"""源泉の取り込み: エージェントによるオントロジーへの追加案（#69）。

エージェントはソースカタログの読み取り tool で源泉を調べ、`submit_proposal` で追加案を出す。
案は既存のオントロジーに適用して検証し、誤りは tool の結果で返して直させる。
ソースデータ（説明・値）はデータとして扱い、指示としては扱わない（catalog の tool が明示する）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool
from pydantic import ValidationError

from adp.catalog.catalog import Catalog
from adp.catalog.tools import SERVER_NAME as CATALOG_SERVER
from adp.catalog.tools import build_tools
from adp.llm import AgentRunner, AgentSpec, LLMSettings, TurnResult, tool_text
from adp.onboarding.prompts import SYSTEM_PROMPT
from adp.onboarding.proposal import OntologyPatch, validate_patch
from adp.ontology.models import Ontology, dump

SERVER_NAME = "onboarding"
MAX_TURNS = 12


@dataclass
class AgentProposal:
    patch: OntologyPatch | None = None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    error: str | None = None


def build_submit_tool(
    ontology: Ontology, catalog: Catalog, source: str, out: AgentProposal
) -> SdkMcpTool[Any]:
    @tool(
        "submit_proposal",
        "オントロジーへの追加案を出す。引数 patch は {domains, concepts, relations, constraints}"
        "（concepts は 1 つ以上）。誤りがあれば理由を返す",
        {"patch": dict},
    )
    async def submit_proposal(args: dict[str, Any]) -> dict[str, Any]:
        out.attempts += 1
        raw = args.get("patch")
        if isinstance(raw, str):  # JSON の文字列で渡されることがある
            try:
                raw = json.loads(raw)
            except ValueError:
                return tool_text("patch を JSON として読めません", error=True)
        try:
            patch = OntologyPatch.model_validate(raw)
        except ValidationError as e:
            out.errors.append(str(e))
            return tool_text(f"形式の誤り: {e}", error=True)
        # DB を読むのでスレッドで（イベントループを止めない）
        errors = await asyncio.to_thread(validate_patch, ontology, patch, catalog, source)
        if errors:
            out.errors += errors
            return tool_text("直してください:\n" + "\n".join(f"- {e}" for e in errors), error=True)
        out.patch = patch
        return tool_text("受け付けました。これで終わりです。")

    return submit_proposal


async def propose_with_agent(
    runner: AgentRunner,
    settings: LLMSettings,
    ontology: Ontology,
    catalog: Catalog,
    source: str,
) -> AgentProposal:
    out = AgentProposal()
    spec = AgentSpec(
        system_prompt=SYSTEM_PROMPT + "\n既存のオントロジー:\n```yaml\n" + dump(ontology) + "```\n",
        model=settings.default_model,
        tools={
            CATALOG_SERVER: build_tools(catalog, sample=False),  # 行の値は要らない（最小権限）
            SERVER_NAME: [build_submit_tool(ontology, catalog, source, out)],
        },
        max_turns=MAX_TURNS,
        purpose="onboarding",
    )
    try:
        async for event in runner.run(
            f"源泉 {source} をオントロジーに追加する案を作ってください。", spec
        ):
            if isinstance(event, TurnResult) and event.is_error:
                out.error = event.error or "エージェントの実行に失敗しました"
    except Exception as e:  # 認証・ネットワークなど。決定的な案に切り替える
        out.error = f"{type(e).__name__}: {e}"
    if out.patch is None and out.error is None:
        out.error = "エージェントが受け付けられる案を出しませんでした"
    return out
