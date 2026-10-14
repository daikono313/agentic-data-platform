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
from adp.interview.wording import has_example_values, is_example_confirmation
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


def build_tools(
    state: InterviewState,
    on_change: Callable[[], None] | None = None,
    issues: Callable[[BeamModel], list[str]] | None = None,
    shown: Callable[[], str] | None = None,
) -> list[SdkMcpTool[Any]]:
    """issues: 確定までに残っている課題（既定は BEAM の条件だけ。設計の課題も含められる）。
    shown: ユーザーに見えている直近の文（直前のアシスタントの発言・ユーザーの発言・今の発言の
    本文）。具体例の確認で、値が見えているかを確かめるのに使う（#163。eval の examples_shown と
    同じ範囲）。"""

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
        return apply(lambda d: upsert(d["events"], "name", args["data"]))

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
        return apply(lambda d: upsert(d["metrics"], "name", args["data"]))

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
        # tool の結果はユーザーに見えないので、値を書かない具体例の確認は確かめようがない（#163）
        if is_example_confirmation(question) and not has_example_values(
            question + "\n" + (shown() if shown else "")
        ):
            return tool_text(
                "具体例の値がユーザーに見えていません。tool の結果はユーザーには見えないため、"
                "確かめてもらう行の値（日時・場所・商品・数量など）を本文に書いてから、"
                "もう一度 present_choices を呼んでください。",
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
