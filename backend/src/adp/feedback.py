"""インタビューへのフィードバック（#44、design-doc の S4）。

発言ごとの 👍 / 👎 と分類・コメント、セッション全体の 5 段階の評価を受け取る。どれも、その発言を
作ったときのプロンプトのバージョンとモデルに結び付けて保存し、改善のサイクル（#45）で集計する。
ユーザーのコメントはデータとして扱い、指示としては扱わない（要約や eval 化でも同じ）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

CATEGORIES: dict[str, str] = {
    "duplicate": "質問が重複している",
    "jargon": "専門用語がわからない",
    "verbose": "質問が長い・多い",
    "unclear": "何を聞かれているかわからない",
    "wrong": "内容が間違っている",
    "choices": "選択肢が合わない",
    "other": "その他",
}
MAX_COMMENT = 2000
RETENTION_DAYS = 180
"""保持期限（O7）。backend の起動時に、これより古いフィードバックを削除する。"""


class FeedbackIn(BaseModel):
    """POST /sessions/{id}/feedback"""

    kind: Literal["message", "session"]
    message_index: int | None = Field(default=None, ge=0)
    """kind=message のとき、会話の何番目の発言か（0 始まり。アシスタントの発言に限る）。"""
    thumb: Literal["up", "down"] | None = None
    """kind=message のとき。"""
    score: int | None = Field(default=None, ge=1, le=5)
    """kind=session のとき（5 段階）。"""
    categories: list[str] = Field(default_factory=list, max_length=len(CATEGORIES))
    comment: str = Field(default="", max_length=MAX_COMMENT)

    @model_validator(mode="after")
    def _check(self) -> FeedbackIn:
        if self.kind == "message":
            if self.message_index is None or self.thumb is None or self.score is not None:
                raise ValueError("発言へのフィードバックには message_index と thumb が必要です")
        elif self.score is None or self.message_index is not None or self.thumb is not None:
            raise ValueError("セッションの評価には score（1〜5）が必要です")
        unknown = set(self.categories) - set(CATEGORIES)
        if unknown:
            raise ValueError(f"分類にないものがあります: {sorted(unknown)}")
        self.categories = sorted(set(self.categories), key=list(CATEGORIES).index)
        self.comment = self.comment.strip()
        return self
