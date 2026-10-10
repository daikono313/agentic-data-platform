"""API を ScriptedRunner と偽パイプラインで通しで動かす。"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adp.api.app import create_app
from adp.api.service import SessionService
from adp.api.store import Store
from adp.beam import load
from adp.interview import Interviewer
from adp.llm import LLMSettings, ScriptedRunner
from adp.pipeline import PipelineResult, StepStatus, StepUpdate

FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "sales_by_store.yml"


def parse_sse(text: str) -> list[tuple[str, Any]]:
    events = []
    for chunk in text.replace("\r\n", "\n").strip().split("\n\n"):
        name, data = "message", ""
        for line in chunk.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if data:
            events.append((name, json.loads(data)))
    return events


@pytest.fixture
def setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    expected = load(FIXTURE).model_dump(mode="json")
    runner = ScriptedRunner(
        [
            [
                ("set_overview", {"name": "sales_by_store", "title": "店舗別の売上"}),
                "言い換えました。",
                (
                    "present_choices",
                    {"question": "これでよいですか？", "options": ["はい", "いいえ"]},
                ),
            ],
            [
                ("upsert_event", {"data": expected["events"][0]}),
                ("upsert_metric", {"data": expected["metrics"][0]}),
                *[("add_gap", {"data": g}) for g in expected["gaps"]],
                ("set_phase", {"phase": "review"}),
                "まとめました。",
            ],
        ]
    )
    calls: list[str] = []

    async def pipeline(beam, on_step):  # type: ignore[no-untyped-def]
        calls.append(beam.name)
        on_step(StepUpdate("worktree", "作業ブランチを作成", StepStatus.DONE, "beam/x"))
        return PipelineResult(ok=True, pr_url="https://github.com/o/r/pull/1")

    store = Store(tmp_path / "app.db")
    service = SessionService(store, Interviewer(runner, LLMSettings()), pipeline)
    return TestClient(create_app(service)), store, calls, runner


def test_interview_finalize_and_job(setup) -> None:  # type: ignore[no-untyped-def]
    client, store, calls, _ = setup
    session = client.post("/sessions", json={"request": "店舗ごとの売上を見たい"}).json()
    sid = session["id"]
    assert session["phase"] == "framing" and session["messages"] == []

    events = parse_sse(
        client.post(f"/sessions/{sid}/messages", json={"text": "店舗ごとの売上を見たい"}).text
    )
    names = [e[0] for e in events]
    assert "text" in names and "choices" in names and names[-1] == "done"
    assert dict(events)["choices"] == {
        "question": "これでよいですか？",
        "options": ["はい", "いいえ"],
    }

    events = parse_sse(client.post(f"/sessions/{sid}/messages", json={"text": "はい"}).text)
    beam_events = [d for n, d in events if n == "beam"]
    assert beam_events[-1]["issues"] == []
    assert ("phase", {"phase": "review"}) in events

    # 保存されていて、別のサービス（再起動後）からも読める
    view = client.get(f"/sessions/{sid}").json()
    assert view["phase"] == "review" and len(view["messages"]) == 4
    assert store.load_session(sid) is not None

    final = client.post(f"/sessions/{sid}/finalize").json()
    assert final["phase"] == "finalized" and final["versions"] == [1]
    job_id = final["job"]["id"]
    job_events = parse_sse(client.get(f"/jobs/{job_id}/events").text)
    assert job_events[-1][0] == "done"
    assert job_events[-1][1]["pr_url"] == "https://github.com/o/r/pull/1"
    assert calls == ["sales_by_store"]
    assert client.post(f"/sessions/{sid}/finalize").status_code == 409


def test_errors(setup) -> None:  # type: ignore[no-untyped-def]
    client, *_ = setup
    assert client.get("/sessions/nope").status_code == 404
    sid = client.post("/sessions", json={"request": "売上"}).json()["id"]
    res = client.post(f"/sessions/{sid}/finalize")
    assert res.status_code == 409 and "確定できません" in res.json()["detail"]
    assert client.post("/sessions", json={"request": ""}).status_code == 422
