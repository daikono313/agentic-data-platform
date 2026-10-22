"""インタビューエージェントが BEAM 成果物を編集する tool。

編集は必ず「コピー → 変更 → 全体を検証 → 差し替え」で行う。検証に失敗した変更は反映せず、
エラーを Claude に返して直させる。各 tool は確定までに残っている課題も返し、
次の質問の手がかりにする。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool
from pydantic import ValidationError

from adp.beam import BeamModel, BusinessEvent, Gap, MetricRequest, to_yaml
from adp.interview.state import InterviewState, Phase
from adp.interview.wording import CONFIRMED, hidden_confirmation
from adp.llm import tool_text

SERVER_NAME = "beam"


def _remaining(beam: BeamModel, check: Callable[[BeamModel], list[str]] | None = None) -> str:
    issues = (check or BeamModel.finalize_issues)(beam)
    if not issues:
        return "確定に必要な情報はそろっています。"
    head = "\n".join(f"- {i}" for i in issues[:15])
    more = f"\n- ほか {len(issues) - 15} 件" if len(issues) > 15 else ""
    return f"確定までに残っている課題（{len(issues)} 件）:\n{head}{more}"


def _schema(model: type[Any]) -> dict[str, Any]:
    """`{"data": <model>}` の入力スキーマ。$ref が解決できるよう $defs はルートに置く。"""
    schema = model.model_json_schema(mode="validation")
    defs = schema.pop("$defs", {})
    return {
        "type": "object",
        "properties": {"data": schema},
        "required": ["data"],
        "$defs": defs,
    }


_EXAMPLES = {
    "examples": "「2024-11-24 に福岡店で 1 個、2025-02-12 に大阪店で 2 個。この 2 件を具体例として"
    "記録してよいですか？」",
    "reframe": "「『月ごとの売上金額はいくらか』という言い換えで合っていますか？」",
}
"""見えていない確認のエラーで示す、中身を書いた question の例（hidden_confirmation の種類ごと）。"""


def _keep_example_kinds(old: list[Any], new: list[Any]) -> list[Any]:
    """new の具体例に kind・note が無ければ、values が同じ old の具体例から引き継ぐ（#142）。"""
    before = {json.dumps(x.get("values"), sort_keys=True): x for x in old if isinstance(x, dict)}
    kept: list[Any] = []
    for x in new:
        prev = (
            before.get(json.dumps(x.get("values"), sort_keys=True)) if isinstance(x, dict) else None
        )
        if prev is not None:
            x = {k: prev[k] for k in ("kind", "note") if k in prev and k not in x} | x
        kept.append(x)
    return kept


def build_tools(
    state: InterviewState,
    on_change: Callable[[], None] | None = None,
    issues: Callable[[BeamModel], list[str]] | None = None,
    shown: Callable[[], tuple[str, str]] | None = None,
) -> list[SdkMcpTool[Any]]:
    """issues: 確定までに残っている課題（既定は BEAM の条件だけ。設計の課題も含められる）。
    shown: ユーザーに見えている直近の文（アシスタントの直前の発言と今の発言の本文、ユーザーの
    発言）。確認の質問で、確かめてもらう中身が見えているかを確かめるのに使う（#163・#165。
    eval の examples_shown・reframe_shown と同じ範囲）。"""

    def apply(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        data = state.beam.model_dump(mode="json")
        try:
            mutate(data)
            updated = BeamModel.model_validate(data)
        except (ValidationError, ValueError, KeyError) as e:
            return tool_text(
                f"変更を反映できませんでした。内容を直して再実行してください。\n{e}", error=True
            )
        updated.finalized_at = None
        state.beam = updated
        if on_change:
            on_change()
        return tool_text("反映しました。\n" + _remaining(updated, issues))

    def upsert(items: list[dict[str, Any]], key: str, item: dict[str, Any]) -> None:
        for i, existing in enumerate(items):
            if existing[key] == item[key]:
                items[i] = item
                return
        items.append(item)

    @tool("get_beam", "現在の BEAM 成果物（YAML）と、確定までに残っている課題を返す", {})
    async def get_beam(args: dict[str, Any]) -> dict[str, Any]:
        return tool_text(to_yaml(state.beam) + "\n" + _remaining(state.beam, issues))

    @tool(
        "set_overview",
        "成果物の名前（snake_case）とタイトル（日本語）を設定する",
        {"name": str, "title": str},
    )
    async def set_overview(args: dict[str, Any]) -> dict[str, Any]:
        return apply(lambda d: d.update(name=args["name"], title=args["title"]))

    @tool(
        "upsert_event",
        "ビジネスイベントを追加、または同じ name のイベントを丸ごと置き換える。"
        "measures / dimensions / examples などは省略せず、イベント全体を渡すこと",
        _schema(BusinessEvent),
    )
    async def upsert_event(args: dict[str, Any]) -> dict[str, Any]:
        def mutate(d: dict[str, Any]) -> None:
            item = args["data"]
            old = next((e for e in d["events"] if e["name"] == item.get("name")), None)
            # 具体例の型と違い（#142）は、後の置き換えで渡し忘れても、値が同じ例から引き継ぐ
            if old and isinstance(item.get("examples"), list):
                item = item | {"examples": _keep_example_kinds(old["examples"], item["examples"])}
            upsert(d["events"], "name", item)

        return apply(mutate)

    @tool("remove_event", "ビジネスイベントを削除する", {"name": str})
    async def remove_event(args: dict[str, Any]) -> dict[str, Any]:
        def mutate(d: dict[str, Any]) -> None:
            d["events"] = [e for e in d["events"] if e["name"] != args["name"]]

        return apply(mutate)

    @tool(
        "upsert_metric",
        "指標の要求を追加、または同じ name の指標を置き換える",
        _schema(MetricRequest),
    )
    async def upsert_metric(args: dict[str, Any]) -> dict[str, Any]:
        def mutate(d: dict[str, Any]) -> None:
            item = args["data"]
            old = next((m for m in d["metrics"] if m["name"] == item.get("name")), None)
            # 目的は framing で 1 回だけ聞くので、後の置き換えで渡し忘れても消さない（#159）
            if old and old.get("purpose") and not item.get("purpose"):
                item = item | {"purpose": old["purpose"]}
            upsert(d["metrics"], "name", item)

        return apply(mutate)

    @tool("remove_metric", "指標の要求を削除する", {"name": str})
    async def remove_metric(args: dict[str, Any]) -> dict[str, Any]:
        def mutate(d: dict[str, Any]) -> None:
            d["metrics"] = [m for m in d["metrics"] if m["name"] != args["name"]]

        return apply(mutate)

    @tool(
        "add_gap",
        "未解決事項を記録する（データなし、未回答、矛盾、後回し）",
        _schema(Gap),
    )
    async def add_gap(args: dict[str, Any]) -> dict[str, Any]:
        return apply(lambda d: d["gaps"].append(args["data"]))

    @tool(
        "remove_gap",
        "解決した未解決事項を削除する。index は get_beam の gaps の 0 始まりの位置",
        {"index": int},
    )
    async def remove_gap(args: dict[str, Any]) -> dict[str, Any]:
        def mutate(d: dict[str, Any]) -> None:
            index = int(args["index"])
            if not 0 <= index < len(d["gaps"]):
                raise ValueError(
                    f"index {index} の未解決事項はありません（0〜{len(d['gaps']) - 1}）"
                )
            d["gaps"].pop(index)

        return apply(mutate)

    @tool(
        "set_phase",
        "インタビューの段階を更新する（UI の進捗表示に使う）。"
        f"値: {', '.join(p.value for p in Phase if p != Phase.FINALIZED)}",
        {"phase": str},
    )
    async def set_phase(args: dict[str, Any]) -> dict[str, Any]:
        try:
            phase = Phase(args["phase"])
        except ValueError:
            return tool_text(f"不明な段階です: {args['phase']}", error=True)
        if phase == Phase.FINALIZED:
            return tool_text("確定はユーザーが UI で行います。review にしてください。", error=True)
        state.phase = phase
        if on_change:
            on_change()
        return tool_text(f"段階を {phase.value} にしました。")

    @tool(
        "present_choices",
        "ユーザーへの質問と、その選択肢（2〜5個、短い日本語）を UI に表示する。"
        "question には質問文そのものを書く（ユーザーにはこの文が表示される）",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question", "options"],
        },
    )
    async def present_choices(args: dict[str, Any]) -> dict[str, Any]:
        options = [str(o) for o in args["options"]][:5]
        question = str(args.get("question") or "").strip()
        # tool の結果や記録した内容はユーザーに見えないので、中身を書かない確認は確かめようが
        # ない（具体例の値は #163、言い換えた問いは #165）。中身は question に入れさせる。本文に
        # 書かせると「上の言い換えで…選択肢から」と本文から指し（#168）、状況の説明も残る（#170）
        kind = hidden_confirmation(question, *(shown() if shown else ("",)))
        if kind:
            return tool_text(
                f"確かめてもらう{CONFIRMED[kind]}がユーザーに見えていません。tool の結果や記録した"
                "内容はユーザーには見えないため、それを question の中に書いて"
                f"（例: {_EXAMPLES[kind]}）、もう一度 present_choices を呼んでください。"
                "本文は前置きだけにする（無ければ書かない）。",
                error=True,
            )
        state.question = question or None
        state.choices = options
        if on_change:
            on_change()
        return tool_text("選択肢を表示しました: " + json.dumps(options, ensure_ascii=False))

    return [
        get_beam,
        set_overview,
        upsert_event,
        remove_event,
        upsert_metric,
        remove_metric,
        add_gap,
        remove_gap,
        set_phase,
        present_choices,
    ]
