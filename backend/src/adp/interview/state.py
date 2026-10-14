"""インタビューの状態。セッションごとに1つ持つ。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from adp.beam import BeamModel


class Phase(StrEnum):
    """BEAM インタビューの段階。

    各段階の目的・聞くことの正本は docs/knowledge/interview.md（見出しと同じ順・
    同じ名前。表示名は PHASE_LABELS と frontend の PHASES。テストで一致を確かめる）。
    """

    FRAMING = "framing"
    EVENT_DISCOVERY = "event_discovery"
    SEVEN_WS = "seven_ws"
    GRAIN = "grain"
    CONFORMANCE = "conformance"
    PRIORITIZATION = "prioritization"
    REVIEW = "review"
    FINALIZED = "finalized"


PHASE_LABELS: dict[Phase, str] = {
    Phase.FRAMING: "要求の言い換え",
    Phase.EVENT_DISCOVERY: "イベントの発見",
    Phase.SEVEN_WS: "7W の深掘り",
    Phase.GRAIN: "粒度と集計方法",
    Phase.CONFORMANCE: "共通ディメンション",
    Phase.PRIORITIZATION: "優先度",
    Phase.REVIEW: "確認",
    Phase.FINALIZED: "確定",
}


class ChatMessage(BaseModel):
    role: str
    """user / assistant"""
    text: str
    question: str | None = None
    """選択肢つきの質問文。本文とは分けて持ち、画面ではボタンの上に出す。"""
    choices: list[str] = Field(default_factory=list)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: str | None = None
    """アシスタントの発言を作ったときの段階（フィードバックの集計用。#44）。"""
    prompt_version: str | None = None
    """アシスタントの発言を作ったときのプロンプトのバージョン。"""
    model: str | None = None
    """アシスタントの発言を作ったモデル。"""
    removed: list[str] = Field(default_factory=list)
    """画面の位置を指すため本文から取り除いた文（eval でモデルの振る舞いを測るために残す。#77）。"""


class InterviewState(BaseModel):
    beam: BeamModel
    phase: Phase = Phase.FRAMING
    choices: list[str] = Field(default_factory=list)
    """直近の質問に対する選択肢（UI のボタン）。"""
    question: str | None = None
    """選択肢と一緒に示した質問文。"""
    messages: list[ChatMessage] = Field(default_factory=list)
    agent_session_id: str | None = None
    """Claude のセッション ID。次のターンで resume する。"""
