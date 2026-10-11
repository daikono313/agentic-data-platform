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


async def test_pr_state_is_cached_and_limited(tmp_path: Path) -> None:
    lookups: list[str] = []

    def pr_state(url: str) -> str | None:
        lookups.append(url)
        return "OPEN" if url.endswith("/1") else "MERGED"

    async def pipeline(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    store = Store(tmp_path / "app.db")
    service = SessionService(store, Interviewer(None, LLMSettings()), pipeline, pr_state=pr_state)  # type: ignore[arg-type]
    budget = [10]
    assert service._cached_pr_state("https://github.com/o/r/pull/1", budget) == "OPEN"
    assert service._cached_pr_state("https://github.com/o/r/pull/2", budget) == "MERGED"
    # 時間内は問い合わせ直さない。マージ済みは時間が過ぎても問い合わせ直さない
    service._cached_pr_state("https://github.com/o/r/pull/1", budget)
    service._pr_cache = {k: (time.monotonic() - 10_000, v[1]) for k, v in service._pr_cache.items()}
    service._cached_pr_state("https://github.com/o/r/pull/2", budget)
    service._cached_pr_state("https://github.com/o/r/pull/1", budget)
    assert lookups == [
        "https://github.com/o/r/pull/1",
        "https://github.com/o/r/pull/2",
        "https://github.com/o/r/pull/1",
    ]
    # 上限を超えたら問い合わせずに、分からない（または前回の結果）
    assert service._cached_pr_state("https://github.com/o/r/pull/3", [0]) is None


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
