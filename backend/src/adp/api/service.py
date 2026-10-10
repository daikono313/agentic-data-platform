"""API の裏側。セッション（インタビュー）と生成ジョブを管理する。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from adp.api.store import FeedbackRow, Store
from adp.beam import BeamModel, build_event_matrix
from adp.feedback import FeedbackIn
from adp.interview import (
    BeamUpdated,
    ChatMessage,
    ChoicesPresented,
    Interviewer,
    InterviewState,
    PhaseChanged,
    TurnDone,
    new_state,
)
from adp.interview.prompts import prompt_version
from adp.llm import TextDelta, ToolCall
from adp.pipeline import STEPS, PipelineResult, StepUpdate

SSEEvent = dict[str, str]
"""sse-starlette に渡す {"event": ..., "data": <JSON 文字列>}。"""

PipelineRunner = Callable[[BeamModel, Callable[[StepUpdate], None]], Awaitable[PipelineResult]]


def _sse(event: str, data: Any) -> SSEEvent:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass
class Job:
    id: str
    session_id: str
    status: str = "running"
    steps: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"step": s, "label": label, "status": "pending", "detail": ""} for s, label in STEPS
        ]
    )
    pr_url: str | None = None
    error: str | None = None
    branch: str | None = None
    subscribers: list[asyncio.Queue[SSEEvent | None]] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "steps": self.steps,
            "pr_url": self.pr_url,
            "error": self.error,
            "branch": self.branch,
        }


EXCERPT = 2000
"""フィードバックに残す発言の抜粋の長さ。"""


def feedback_view(row: FeedbackRow) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "message_index": row.message_index,
        "thumb": row.thumb,
        "score": row.score,
        "categories": json.loads(row.categories_json),
        "comment": row.comment,
        "prompt_version": row.prompt_version,
        "model": row.model,
    }


class SessionService:
    def __init__(self, store: Store, interviewer: Interviewer, pipeline: PipelineRunner) -> None:
        self.store = store
        self.interviewer = interviewer
        self.pipeline = pipeline
        self._states: dict[str, InterviewState] = {}
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        store.fail_stale_jobs()

    # --- セッション ------------------------------------------------------------------------

    def _state(self, session_id: str) -> InterviewState:
        if session_id not in self._states:
            state = self.store.load_session(session_id)
            if state is None:
                raise NotFound(f"セッション {session_id} がありません")
            self._states[session_id] = state
        return self._states[session_id]

    def create(self, request: str) -> str:
        session_id = uuid.uuid4().hex[:12]
        state = new_state(request.strip())
        self._states[session_id] = state
        self.store.save_session(session_id, state)
        return session_id

    def view(self, session_id: str) -> dict[str, Any]:
        state = self._state(session_id)
        job = self._job_view(session_id)
        return {
            "id": session_id,
            "phase": state.phase.value,
            "beam": state.beam.model_dump(mode="json"),
            "matrix": build_event_matrix(state.beam).model_dump(mode="json"),
            "issues": self.interviewer.issues(state.beam),
            "choices": state.choices,
            "question": state.question,
            "messages": [m.model_dump(mode="json") for m in state.messages],
            "job": job,
            "versions": [v.version for v in self.store.versions(session_id)],
            "feedback": [feedback_view(f) for f in self.store.feedback(session_id)],
        }

    def feedback(self, session_id: str, body: FeedbackIn) -> dict[str, Any]:
        """発言ごと・セッション全体のフィードバックを保存する（#44）。

        その発言を作ったときの段階・質問・選択肢・プロンプトのバージョン・モデルを一緒に残す。
        """
        state = self._state(session_id)
        assistant = [m for m in state.messages if m.role == "assistant"]
        message: ChatMessage | None
        if body.kind == "message":
            i = body.message_index
            if i is None or i >= len(state.messages) or state.messages[i].role != "assistant":
                raise ValueError("その発言はアシスタントの発言ではありません")
            message = state.messages[i]
            user = next((m.text for m in reversed(state.messages[:i]) if m.role == "user"), "")
            phase = message.phase
        else:
            message = assistant[-1] if assistant else None
            user = ""
            phase = state.phase.value
        row = FeedbackRow(
            session_id=session_id,
            kind=body.kind,
            message_index=body.message_index,
            thumb=body.thumb,
            score=body.score,
            categories_json=json.dumps(body.categories),
            comment=body.comment,
            phase=phase,
            question=message.question if message and body.kind == "message" else None,
            choices_json=json.dumps(
                message.choices if message and body.kind == "message" else [],
                ensure_ascii=False,
            ),
            message_text=(message.text if message and body.kind == "message" else "")[:EXCERPT],
            user_text=user[:EXCERPT],
            prompt_version=message.prompt_version if message else prompt_version(),
            model=message.model if message else None,
        )
        # 送られてこなかった分類・コメントは既存の値を残す
        keep = {
            column
            for field, column in (("categories", "categories_json"), ("comment", "comment"))
            if field not in body.model_fields_set
        }
        return feedback_view(self.store.save_feedback(row, keep))

    async def send(self, session_id: str, text: str) -> AsyncIterator[SSEEvent]:
        state = self._state(session_id)
        try:
            async for event in self._send(session_id, state, text):
                yield event
        finally:  # 切断で中断されても、ここまでの会話と成果物を残す
            self.store.save_session(session_id, state)

    async def _send(
        self, session_id: str, state: InterviewState, text: str
    ) -> AsyncIterator[SSEEvent]:
        try:
            async for event in self.interviewer.send(state, text):
                if isinstance(event, TextDelta):
                    yield _sse("text", {"text": event.text})
                elif isinstance(event, ToolCall):
                    yield _sse("tool", {"name": event.name})
                elif isinstance(event, BeamUpdated):
                    self.store.save_session(session_id, state)
                    yield _sse(
                        "beam",
                        {
                            "beam": event.beam.model_dump(mode="json"),
                            "matrix": event.matrix.model_dump(mode="json"),
                            "issues": event.issues,
                        },
                    )
                elif isinstance(event, PhaseChanged):
                    yield _sse("phase", {"phase": event.phase.value})
                elif isinstance(event, ChoicesPresented):
                    yield _sse("choices", {"question": event.question, "options": event.options})
                elif isinstance(event, TurnDone):
                    self.store.save_session(session_id, state)
                    yield _sse(
                        "done",
                        {
                            "message": event.message.model_dump(mode="json"),
                            "error": event.result.error if event.result.is_error else None,
                        },
                    )
        except (ValueError, RuntimeError) as e:  # 確定済み・処理中など
            raise Conflict(str(e)) from e

    # --- 確定と生成ジョブ --------------------------------------------------------------------

    def finalize(self, session_id: str) -> dict[str, Any]:
        state = self._state(session_id)
        if state.phase.value == "finalized":
            raise Conflict("確定済みのセッションです")
        if self.interviewer.is_busy(state):
            raise Conflict("応答の途中です。応答が終わってから確定してください")
        running = self._job_view(session_id)
        if running and running["status"] == "running":
            raise Conflict("生成ジョブを実行中です")
        try:
            beam = self.interviewer.finalize(state)
        except ValueError as e:
            raise Conflict(str(e)) from e
        self.store.save_session(session_id, state)
        self.store.add_version(session_id, beam)
        job = Job(id=uuid.uuid4().hex[:12], session_id=session_id)
        self._jobs[job.id] = job
        self.store.save_job(job.id, session_id, job.status, job.steps)
        task = asyncio.get_running_loop().create_task(self._run_job(job, beam))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self.view(session_id)

    def reopen(self, session_id: str) -> dict[str, Any]:
        """確定を取り消して修正できるようにする。生成ジョブの実行中はできない。"""
        state = self._state(session_id)
        running = self._job_view(session_id)
        if running and running["status"] == "running":
            raise Conflict("生成ジョブを実行中です")
        try:
            self.interviewer.reopen(state)
        except ValueError as e:
            raise Conflict(str(e)) from e
        self.store.save_session(session_id, state)
        return self.view(session_id)

    def job_branch(self, job_id: str) -> str | None:
        """PR を作った生成ジョブの作業ブランチ（描画画面のプレビュー用）。"""
        job = self._jobs.get(job_id)
        if job is not None:
            return job.branch if job.pr_url else None
        row = self.store.job(job_id)
        return row.branch if row is not None and row.pr_url else None

    def has_job(self, job_id: str) -> bool:
        return job_id in self._jobs or self.store.job(job_id) is not None

    async def _run_job(self, job: Job, beam: BeamModel) -> None:
        def on_step(update: StepUpdate) -> None:
            data = {
                "step": update.step,
                "label": update.label,
                "status": update.status.value,
                "detail": update.detail,
            }
            job.steps = [data if s["step"] == update.step else s for s in job.steps]
            self.store.save_job(job.id, job.session_id, job.status, job.steps)
            self._publish(job, _sse("step", data))

        try:
            result = await self.pipeline(beam, on_step)
            job.status = "succeeded" if result.ok else "failed"
            job.pr_url, job.error, job.branch = result.pr_url, result.error, result.branch
        except Exception as e:
            job.status, job.error = "failed", str(e)
        try:
            self.store.save_job(
                job.id, job.session_id, job.status, job.steps, job.pr_url, job.error, job.branch
            )
        finally:
            self._publish(job, _sse("done", job.view()))
            for q in job.subscribers:
                q.put_nowait(None)

    def _publish(self, job: Job, event: SSEEvent) -> None:
        for q in job.subscribers:
            q.put_nowait(event)

    def _job_view(self, session_id: str) -> dict[str, Any] | None:
        live = [j for j in self._jobs.values() if j.session_id == session_id]
        if live:
            return live[-1].view()
        row = self.store.latest_job(session_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "status": row.status,
            "steps": json.loads(row.steps_json),
            "pr_url": row.pr_url,
            "error": row.error,
            "branch": row.branch,
        }

    async def job_events(self, job_id: str) -> AsyncIterator[SSEEvent]:
        job = self._jobs.get(job_id)
        if job is None:
            row = self.store.job(job_id)
            if row is None:
                raise NotFound(f"ジョブ {job_id} がありません")
            yield _sse(
                "done",
                {
                    "id": row.id,
                    "status": row.status,
                    "steps": json.loads(row.steps_json),
                    "pr_url": row.pr_url,
                    "error": row.error,
                    "branch": row.branch,
                },
            )
            return
        for step in job.steps:
            yield _sse("step", step)
        if job.status != "running":
            yield _sse("done", job.view())
            return
        queue: asyncio.Queue[SSEEvent | None] = asyncio.Queue()
        job.subscribers.append(queue)
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            job.subscribers.remove(queue)
