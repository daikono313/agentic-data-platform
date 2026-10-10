"""インタビューへのフィードバック（#44）。

発言ごとの 👍 / 👎・分類・コメントと、セッション全体の評価。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session

from adp.api.store import FeedbackRow, Store
from adp.interview.prompts import PROMPT_VERSION, prompt_version
from tests.api.test_api import parse_sse, setup  # noqa: F401  （fixture）


def _interview(client) -> tuple[str, list[dict[str, Any]]]:  # type: ignore[no-untyped-def]
    sid = client.post("/sessions", json={"request": "店舗ごとの売上を見たい"}).json()["id"]
    parse_sse(client.post(f"/sessions/{sid}/messages", json={"text": "店舗ごとの売上"}).text)
    return sid, client.get(f"/sessions/{sid}").json()["messages"]


def test_message_feedback_is_saved_with_context(setup) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    client, store, _, _ = setup
    sid, messages = _interview(client)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    reply = messages[1]
    # 発言には、作ったときの段階・プロンプトのバージョン・モデルが残る
    assert reply["prompt_version"] == prompt_version()
    assert reply["prompt_version"].startswith(PROMPT_VERSION + "+")
    assert reply["phase"] == "framing" and reply["model"]

    res = client.post(
        f"/sessions/{sid}/feedback",
        json={
            "kind": "message",
            "message_index": 1,
            "thumb": "down",
            "categories": ["jargon", "duplicate", "jargon"],
            "comment": " 『粒度』の意味がわからない ",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["categories"] == ["duplicate", "jargon"]

    [row] = store.feedback(sid)
    assert row.kind == "message" and row.message_index == 1 and row.thumb == "down"
    assert row.comment == "『粒度』の意味がわからない"
    assert row.question == "これでよいですか？" and "はい" in row.choices_json
    assert row.message_text == "言い換えました。" and row.user_text == "店舗ごとの売上"
    assert row.prompt_version == prompt_version() and row.phase == "framing"

    # 送り直すと上書きする（同じ発言に 2 件残さない）
    client.post(
        f"/sessions/{sid}/feedback", json={"kind": "message", "message_index": 1, "thumb": "up"}
    )
    [row] = store.feedback(sid)
    assert row.thumb == "up" and row.comment == ""
    assert client.get(f"/sessions/{sid}").json()["feedback"][0]["thumb"] == "up"


def test_session_score(setup) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    client, store, _, _ = setup
    sid, _ = _interview(client)
    for score in (2, 4):
        res = client.post(
            f"/sessions/{sid}/feedback", json={"kind": "session", "score": score, "comment": "x"}
        )
        assert res.status_code == 200, res.text
    [row] = store.feedback(sid)
    assert row.kind == "session" and row.score == 4 and row.message_index is None
    assert row.prompt_version == prompt_version()


def test_invalid_feedback_is_rejected(setup) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    client, store, _, _ = setup
    sid, _ = _interview(client)
    bad = [
        {"kind": "message", "message_index": 0, "thumb": "up"},  # ユーザーの発言
        {"kind": "message", "message_index": 9, "thumb": "up"},  # 範囲外
        {"kind": "message", "message_index": 1},  # thumb が無い
        {"kind": "message", "message_index": 1, "thumb": "meh"},
        {"kind": "message", "message_index": 1, "thumb": "up", "categories": ["nope"]},
        {"kind": "message", "message_index": 1, "thumb": "up", "comment": "x" * 2001},
        {"kind": "session", "score": 6},
        {"kind": "session", "score": 3, "thumb": "up"},
    ]
    for body in bad:
        assert client.post(f"/sessions/{sid}/feedback", json=body).status_code == 422, body
    assert client.post("/sessions/nope/feedback", json=bad[0]).status_code == 404
    assert store.feedback(sid) == []
    assert "jargon" in client.get("/feedback/categories").json()


def _age(store: Store, row: FeedbackRow, days: int) -> None:
    with Session(store.engine) as db:
        saved = db.get(FeedbackRow, row.id)
        assert saved is not None
        saved.updated_at = datetime.now(UTC) - timedelta(days=days)
        db.add(saved)
        db.commit()


def test_old_feedback_is_pruned(tmp_path: Path) -> None:
    """保持期限（O7）を過ぎたフィードバックは起動時に削除する。"""
    store = Store(tmp_path / "app.db")
    store.save_feedback(FeedbackRow(session_id="s", kind="session", score=1))
    _age(store, store.save_feedback(FeedbackRow(session_id="s2", kind="session", score=5)), 181)
    _age(store, store.save_feedback(FeedbackRow(session_id="s3", kind="session", score=5)), 179)
    assert len(store.feedback()) == 3
    Store(tmp_path / "app.db")
    assert [r.session_id for r in store.feedback()] == ["s", "s3"]
    # 0 なら削除しない
    _age(store, store.feedback()[1], 400)
    Store(tmp_path / "app.db", feedback_retention_days=0)
    assert len(store.feedback()) == 2
