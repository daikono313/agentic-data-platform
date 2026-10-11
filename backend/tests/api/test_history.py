"""これまでの要求と PR の一覧（#73）。"""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from adp.api.app import create_app
from adp.api.service import SessionService
from adp.api.store import Store
from adp.interview import Interviewer
from adp.llm import LLMSettings
from adp.vcs import GitHub
from tests.api.test_api import parse_sse, setup  # noqa: F401  （fixture）


def test_history_lists_sessions_with_job_and_pr_state(setup) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    client, store, _, runner = setup
    sid = client.post("/sessions", json={"request": "店舗ごとの売上を見たい"}).json()["id"]
    for text in ("店舗ごとの売上", "はい"):
        parse_sse(client.post(f"/sessions/{sid}/messages", json={"text": text}).text)
    other = client.post("/sessions", json={"request": "返品率を見たい"}).json()["id"]

    body = client.get("/sessions").json()
    assert [s["id"] for s in body["sessions"]] == [other, sid]  # 新しく更新された順
    first = body["sessions"][1]
    assert first["title"] == "店舗別の売上" and first["phase"] == "review"
    assert first["metrics"] == ["売上金額"] and first["job"] is None and first["pr_state"] is None


def _service(tmp_path: Path, pr_state):  # type: ignore[no-untyped-def]
    async def pipeline(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    store = Store(tmp_path / "app.db")
    interviewer = Interviewer(None, LLMSettings())  # type: ignore[arg-type]
    return SessionService(store, interviewer, pipeline, pr_state=pr_state), store


def test_history_includes_pr_state_and_caches_it(tmp_path: Path) -> None:
    lookups: list[str] = []

    def pr_state(url: str) -> str | None:
        lookups.append(url)
        return "OPEN" if url.endswith("/1") else "MERGED"

    service, store = _service(tmp_path, pr_state)
    from adp.beam import BeamModel
    from adp.interview.state import InterviewState

    for sid, n in (("a", 1), ("b", 2), ("c", 2)):
        store.save_session(
            sid,
            InterviewState(
                beam=BeamModel(name=f"s_{sid}", title=f"要求 {sid}", request=f"要求 {sid}")
            ),
        )
        store.save_job(f"j{sid}", sid, "succeeded", [], f"https://github.com/o/r/pull/{n}")
    states = {s["id"]: s["pr_state"] for s in service.history()["sessions"]}
    assert states == {"a": "OPEN", "b": "MERGED", "c": "MERGED"}
    assert sorted(lookups) == ["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"]
    # 時間内は問い合わせ直さない。マージ済みは時間が過ぎても問い合わせ直さない
    service.history()
    service._pr_cache = {k: (time.monotonic() - 10_000, v[1]) for k, v in service._pr_cache.items()}
    service.history()
    assert sorted(lookups) == [
        "https://github.com/o/r/pull/1",
        "https://github.com/o/r/pull/1",
        "https://github.com/o/r/pull/2",
    ]


def test_slow_github_does_not_block_the_list(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import threading

    from adp.api import service as service_module

    release = threading.Event()

    def pr_state(url: str) -> str | None:
        release.wait(5)
        return "OPEN"

    monkeypatch.setattr(service_module, "PR_STATE_DEADLINE_S", 0.2)
    service, store = _service(tmp_path, pr_state)
    from adp.beam import BeamModel
    from adp.interview.state import InterviewState

    store.save_session(
        "a", InterviewState(beam=BeamModel(name="s_a", title="要求", request="要求"))
    )
    store.save_job("ja", "a", "succeeded", [], "https://github.com/o/r/pull/1")
    started = time.monotonic()
    [row] = service.history()["sessions"]
    assert time.monotonic() - started < 2 and row["pr_state"] is None
    release.set()


def test_broken_session_row_does_not_break_the_list(tmp_path: Path) -> None:
    from sqlmodel import Session

    from adp.api.store import SessionRow

    service, store = _service(tmp_path, None)
    with Session(store.engine) as db:
        db.add(SessionRow(id="broken", state_json='{"beam": 1}'))
        db.commit()
    [row] = service.history()["sessions"]
    assert row["id"] == "broken" and row["phase"] == "unknown"


def test_github_pr_state_only_queries_github_pr_urls(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake(args, cwd, env):  # type: ignore[no-untyped-def]
        import subprocess

        calls.append(list(args))
        out = "tok" if args[:3] == ["gh", "auth", "token"] else "MERGED\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    gh = GitHub(tmp_path, "daikono313", fake)
    assert gh.pr_state("https://github.com/o/r/pull/12") == "MERGED"
    for bad in (
        "(dry-run) beam/x",
        "https://example.com/o/r/pull/1",
        "--web",
        "https://github.com/o/r/pull/1;x",
    ):
        assert gh.pr_state(bad) is None
    assert sum(1 for c in calls if c[:3] == ["gh", "pr", "view"]) == 1


def test_history_endpoint_without_github(tmp_path: Path) -> None:
    async def pipeline(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(Store(tmp_path / "app.db"), Interviewer(None, LLMSettings()), pipeline)  # type: ignore[arg-type]
    assert TestClient(create_app(service)).get("/sessions").json() == {"sessions": []}
