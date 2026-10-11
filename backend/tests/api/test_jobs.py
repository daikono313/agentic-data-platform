"""ジョブとセッションの管理（#110）: ジョブの作り方・表示・一覧の問い合わせ・メモリの上限。"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from adp.api import service as service_module
from adp.api.service import Job, SessionService
from adp.api.store import SessionRow, Store
from adp.beam import load
from adp.interview import Interviewer
from adp.interview.state import InterviewState, Phase
from adp.llm import LLMSettings
from adp.pipeline import STEPS, PipelineResult, StepStatus, StepUpdate
from tests.api.test_api import FIXTURE


def _service(tmp_path: Path, pipeline: Any = None, **kwargs: Any) -> SessionService:
    async def never(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    interviewer = Interviewer(None, LLMSettings())  # type: ignore[arg-type]
    return SessionService(Store(tmp_path / "app.db"), interviewer, pipeline or never, **kwargs)


def test_job_new_lists_pending_steps() -> None:
    job = Job.new("s1", [("a", "A"), ("b", "B")])
    assert job.session_id == "s1" and job.status == "running" and len(job.id) == 12
    assert job.steps == [
        {"step": "a", "label": "A", "status": "pending", "detail": ""},
        {"step": "b", "label": "B", "status": "pending", "detail": ""},
    ]
    assert Job.new("s1", STEPS).id != job.id


def test_stored_job_view_matches_live_view(tmp_path: Path) -> None:
    store = Store(tmp_path / "app.db")
    job = Job.new("s1", [("a", "A")])
    job.status, job.pr_url, job.error, job.branch = "failed", "u", "e", "beam/x"
    store.save_job(job.id, job.session_id, job.status, job.steps, job.pr_url, job.error, job.branch)
    row = store.job(job.id)
    assert row is not None and row.view() == job.view()


async def test_finished_job_is_dropped_from_memory_and_read_from_db(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def pipeline(beam: Any, on_step: Callable[[StepUpdate], None]) -> PipelineResult:
        on_step(StepUpdate("worktree", "作業ブランチを作成", StepStatus.DONE, "beam/x"))
        await release.wait()
        return PipelineResult(ok=True, pr_url="https://github.com/o/r/pull/1", branch="beam/x")

    service = _service(tmp_path, pipeline)
    job = Job.new("s1", STEPS)
    service._start(job, lambda on_step: pipeline(None, on_step))
    await asyncio.sleep(0)
    assert service._jobs == {job.id: job} and service._running() == {"s1"}
    events = service.job_events(job.id)
    first = [await anext(events) for _ in STEPS]  # いまのステップを先に流す
    assert first[0]["event"] == "step"
    release.set()
    await asyncio.gather(*service._tasks)
    # 購読していた側にも done が届き、メモリからは外れる
    rest = [e async for e in events]
    assert rest[-1]["event"] == "done" and '"succeeded"' in rest[-1]["data"]
    assert service._jobs == {} and service._running() == set()
    assert service._job_view("s1") == service.store.job(job.id).view()  # type: ignore[union-attr]
    assert service.job_branch(job.id) == "beam/x" and service.has_job(job.id)
    replay = [e async for e in service.job_events(job.id)]
    assert [e["event"] for e in replay] == ["done"]


async def test_running_checks_use_live_jobs(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def slow(on_step):  # type: ignore[no-untyped-def]
        await release.wait()
        return PipelineResult(ok=True)

    service = _service(tmp_path, sync=slow)
    service.start_sync()
    with pytest.raises(service_module.Conflict, match="反映を実行中"):
        service.start_sync()
    release.set()
    await asyncio.gather(*service._tasks)
    assert service.sync_job()["status"] == "succeeded"  # type: ignore[index]
    service.start_sync()  # 終わったら（メモリから外れても）また始められる
    await asyncio.gather(*service._tasks)


def _state(title: str, phase: Phase = Phase.REVIEW) -> InterviewState:
    beam = load(FIXTURE).model_copy(update={"title": title, "request": f"{title}を見たい"})
    return InterviewState(beam=beam, phase=phase)


def test_history_reads_summaries_and_jobs_in_bulk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    store = service.store
    for sid in ("a", "b", "c"):
        store.save_session(sid, _state(f"要求 {sid}"))
    store.save_job("old", "a", "failed", [])
    store.save_job("new", "a", "succeeded", [], "https://github.com/o/r/pull/1")
    with Session(store.engine) as db:
        db.add(SessionRow(id="notjson", state_json="{not json"))
        db.commit()

    calls: list[list[str]] = []
    latest = store.latest_jobs

    def counting(ids: list[str]) -> Any:
        calls.append(ids)
        return latest(ids)

    monkeypatch.setattr(store, "latest_jobs", counting)

    def fail(*args: Any) -> None:
        raise AssertionError("一覧では状態を検証しない")

    monkeypatch.setattr(InterviewState, "model_validate_json", fail)
    rows = {s["id"]: s for s in service.history()["sessions"]}
    assert len(calls) == 1 and sorted(calls[0]) == ["a", "b", "c", "notjson"]
    assert rows["a"]["title"] == "要求 a" and rows["a"]["request"] == "要求 aを見たい"
    assert rows["a"]["phase"] == "review" and rows["a"]["metrics"] == ["売上金額"]
    assert rows["a"]["job"]["id"] == "new" and rows["b"]["job"] is None
    assert rows["notjson"]["phase"] == "unknown"
    assert rows["notjson"]["request"] == "（読めないセッション）"


def test_state_cache_is_bounded_but_keeps_busy_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "STATE_CACHE", 2)
    service = _service(tmp_path)
    busy: set[int] = set()
    monkeypatch.setattr(service.interviewer, "is_busy", lambda s: id(s) in busy)
    first = service.create("一つ目")
    busy.add(id(service._states[first]))
    second = service.create("二つ目")
    third = service.create("三つ目")
    # 応答中の一つ目は残し、使っていない二つ目を外す
    assert list(service._states) == [first, third]
    # 外したセッションも DB から読み直せる
    assert service.view(second)["id"] == second
    assert list(service._states) == [first, second]


def test_history_prefers_running_job_in_memory_and_skips_stale_leftovers(tmp_path: Path) -> None:
    service = _service(tmp_path)
    store = service.store
    for sid in ("a", "b"):
        store.save_session(sid, _state(f"要求 {sid}"))
    store.save_job("old", "a", "succeeded", [])
    running = Job.new("a", [("x", "X")])
    service._jobs[running.id] = running
    # 保存に失敗して残った終わったジョブは、新しい DB の行を隠さない（#110 レビュー）
    leftover = Job(id="left", session_id="b", status="failed")
    service._jobs["left"] = leftover
    store.save_job("newer", "b", "succeeded", [])
    rows = {s["id"]: s for s in service.history()["sessions"]}
    assert rows["a"]["job"]["id"] == running.id and rows["a"]["job"]["status"] == "running"
    assert rows["b"]["job"]["id"] == "newer"


def test_history_marks_rows_with_unknown_shape_unreadable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with Session(service.store.engine) as db:
        db.add(SessionRow(id="array", state_json='{"beam": [], "phase": "review"}'))
        db.add(SessionRow(id="bogus", state_json='{"beam": {"title": "t"}, "phase": "bogus"}'))
        db.add(SessionRow(id="empty", state_json=""))
        db.commit()
    rows = service.history()["sessions"]
    assert {r["id"]: r["phase"] for r in rows} == {
        "array": "unknown",
        "bogus": "unknown",
        "empty": "unknown",
    }


def test_state_cache_is_safe_across_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同期のエンドポイントはスレッドで動く。外すのと読むのが重なっても落ちない。"""
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(service_module, "STATE_CACHE", 2)
    service = _service(tmp_path)
    ids = [service.create(f"要求 {i}") for i in range(8)]

    def touch(i: int) -> str:
        return str(service.view(ids[i % len(ids)])["id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(set(pool.map(touch, range(400)))) == sorted(ids)
    assert len(service._states) <= 2


async def test_state_being_sent_is_not_evicted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """応答を始めたセッションは、応答のロックを取る前でもキャッシュから外さない。"""
    monkeypatch.setattr(service_module, "STATE_CACHE", 1)
    service = _service(tmp_path)
    sid = service.create("一つ目")
    state = service._states[sid]
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_send(session_id, st, text):  # type: ignore[no-untyped-def]
        started.set()
        await release.wait()
        yield {"event": "done", "data": "{}"}

    monkeypatch.setattr(service, "_send", fake_send)

    async def consume() -> list[dict[str, str]]:
        return [e async for e in service.send(sid, "こんにちは")]

    task = asyncio.create_task(consume())
    await started.wait()
    service.create("二つ目")  # 上限 1 でも、送信中の一つ目は外さない
    assert service._states.get(sid) is state
    release.set()
    assert len(await task) == 1
    assert service._in_use == {}


def test_interviewer_forgets_locks_of_evicted_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "STATE_CACHE", 1)
    service = _service(tmp_path)
    first = service.create("一つ目")
    state = service._states[first]
    service.interviewer._locks[id(state)] = asyncio.Lock()
    service.create("二つ目")
    assert id(state) not in service.interviewer._locks


def test_old_activity_is_pruned(tmp_path: Path) -> None:
    service = _service(tmp_path)
    import time

    now = time.monotonic()
    service._activity = {"old": now - 2 * service_module.ACTIVITY_KEEP_S, "new": now - 120}
    assert service.interview_activity(60) is None and service._activity == {"new": now - 120}
