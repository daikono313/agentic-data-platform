"""API の裏側。セッション（インタビュー）と生成ジョブを管理する。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

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
from adp.onboarding.pipeline import ONBOARD_STEPS
from adp.onboarding.proposal import OntologyPatch
from adp.pipeline import STEPS, PipelineResult, StepUpdate
from adp.sync import SYNC_STEPS

SSEEvent = dict[str, str]
"""sse-starlette に渡す {"event": ..., "data": <JSON 文字列>}。"""

PipelineRunner = Callable[[BeamModel, Callable[[StepUpdate], None]], Awaitable[PipelineResult]]
OnboardingRunner = Callable[
    [str, OntologyPatch, str, Callable[[StepUpdate], None]], Awaitable[PipelineResult]
]
"""(源泉, 追加案, クレンジングの定義の YAML, on_step) → 結果（#69）。"""
SyncRunner = Callable[[Callable[[StepUpdate], None]], Awaitable[PipelineResult]]
SYNC_ID = "sync:main"
"""マージ後の反映ジョブの session_id（#74）。"""
ONBOARD_PREFIX = "onboard:"
"""取り込みジョブの session_id（セッションの代わりに源泉で引く）。"""
PrState = Callable[[str], str | None]
"""PR の URL → OPEN / MERGED / CLOSED（取れなければ None）。"""
PR_STATE_TTL_S = 300.0
PR_STATE_LOOKUPS = 20
"""一覧 1 回あたりに GitHub へ問い合わせる PR の数の上限（残りは前回の結果か不明）。"""
PR_STATE_DEADLINE_S = 15.0
"""PR の状態の問い合わせを待つ時間の上限。過ぎたら残りは「不明」で一覧を返す（#88 レビュー）。"""
PR_STATE_WORKERS = 5
STATE_CACHE = 64
"""メモリに置くインタビューの状態の数の上限。超えたら使っていないものから外す（DB から読める）。"""


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
    steps: list[dict[str, Any]] = field(default_factory=list)
    pr_url: str | None = None
    error: str | None = None
    branch: str | None = None
    subscribers: list[asyncio.Queue[SSEEvent | None]] = field(default_factory=list)

    @classmethod
    def new(cls, session_id: str, steps: Sequence[tuple[str, str]]) -> Job:
        """新しいジョブ。steps は (ステップ, 表示名) の並び（STEPS・ONBOARD_STEPS・SYNC_STEPS）。"""
        return cls(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            steps=[
                {"step": step, "label": label, "status": "pending", "detail": ""}
                for step, label in steps
            ],
        )

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
    def __init__(
        self,
        store: Store,
        interviewer: Interviewer,
        pipeline: PipelineRunner,
        onboarding: OnboardingRunner | None = None,
        pr_state: PrState | None = None,
        sync: SyncRunner | None = None,
    ) -> None:
        self.store = store
        self.interviewer = interviewer
        self.pipeline = pipeline
        self.onboarding = onboarding
        self.pr_state = pr_state
        self.sync = sync
        self._pr_cache: dict[str, tuple[float, str | None]] = {}
        self._states: OrderedDict[str, InterviewState] = OrderedDict()
        """最近使った順（STATE_CACHE 件まで）。"""
        self._jobs: dict[str, Job] = {}
        """実行中のジョブ。終わって保存したら外す（以後は DB から読む）。"""
        self._tasks: set[asyncio.Task[None]] = set()
        self._activity: dict[str, float] = {}
        """インタビューの直近の操作（time.monotonic）。自動反映を控えるため（#106）。"""
        store.fail_stale_jobs()

    # --- セッション ------------------------------------------------------------------------

    def _state(self, session_id: str) -> InterviewState:
        if session_id in self._states:
            self._states.move_to_end(session_id)
        else:
            try:
                state = self.store.load_session(session_id)
            except ValidationError as e:  # 検証が厳しくなる前に保存した成果物など（500 にしない）
                raise NotFound(
                    f"セッション {session_id} は今の形式で読めません（成果物の検証に通りません）。"
                    "新しく要求を始めてください"
                ) from e
            if state is None:
                raise NotFound(f"セッション {session_id} がありません")
            self._cache_state(session_id, state)
        return self._states[session_id]

    def _cache_state(self, session_id: str, state: InterviewState) -> None:
        """状態をメモリに置き、上限を超えたら古いものから外す。

        応答の途中の状態は外さない（応答の排他は状態のオブジェクトごとのため）。外した状態は
        保存済みなので、次に使うときに DB から読み直す。
        """
        self._states[session_id] = state
        for sid in list(self._states):
            if len(self._states) <= STATE_CACHE:
                break
            if sid != session_id and not self.interviewer.is_busy(self._states[sid]):
                del self._states[sid]

    def create(self, request: str) -> str:
        session_id = uuid.uuid4().hex[:12]
        state = new_state(request.strip())
        self._cache_state(session_id, state)
        self.store.save_session(session_id, state)
        self._activity[session_id] = time.monotonic()
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

    # --- 一覧（#73） ----------------------------------------------------------------------

    def history(self) -> dict[str, Any]:
        """これまでのセッション（要求・段階・生成ジョブ・PR の状態）の新しい順。

        PR の状態は GitHub に問い合わせる（同期処理。呼び出し側でスレッドに出す）。
        """
        rows = self.store.session_summaries()
        jobs = self._job_views([row.id for row in rows])
        sessions: list[dict[str, Any]] = []
        for row in rows:
            readable = row.phase is not None  # 壊れた行でも一覧全体は返す
            sessions.append(
                {
                    "id": row.id,
                    "title": row.title,
                    "request": row.request if readable else "（読めないセッション）",
                    "phase": row.phase if readable else "unknown",
                    "updated_at": row.updated_at.isoformat(),
                    "metrics": row.metrics,
                    "job": jobs.get(row.id),
                    "pr_state": None,
                }
            )
        urls = list(
            dict.fromkeys(s["job"]["pr_url"] for s in sessions if s["job"] and s["job"]["pr_url"])
        )
        states = self._pr_states(urls)
        for s in sessions:
            if s["job"] and s["job"]["pr_url"]:
                s["pr_state"] = states.get(s["job"]["pr_url"])
        return {"sessions": sessions}

    def _pr_states(self, urls: list[str]) -> dict[str, str | None]:
        """PR の状態をまとめて調べる。キャッシュに無いものは並列に、決まった時間まで待つ。"""
        now = time.monotonic()
        result: dict[str, str | None] = {}
        todo: list[str] = []
        for url in urls:
            cached = self._pr_cache.get(url)
            # マージ・クローズ済みは変わらないので問い合わせ直さない
            if cached and (cached[1] in ("MERGED", "CLOSED") or now - cached[0] < PR_STATE_TTL_S):
                result[url] = cached[1]
            else:
                result[url] = cached[1] if cached else None
                todo.append(url)
        lookup = self.pr_state
        if lookup is None or not todo:
            return result
        pool = ThreadPoolExecutor(max_workers=PR_STATE_WORKERS)
        futures = {pool.submit(lookup, url): url for url in todo[:PR_STATE_LOOKUPS]}
        try:
            for future in as_completed(futures, timeout=PR_STATE_DEADLINE_S):
                url = futures[future]
                try:
                    state = future.result()
                except Exception:
                    state = None
                self._pr_cache[url] = (time.monotonic(), state)
                result[url] = state
        except FuturesTimeout:  # 残りは前回の結果か不明のまま返す（問い合わせは後ろで続く）
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return result

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
        # 送られてこなかった分類・コメントは既存の値を残す。ただし 👍 に付け替えたときは、👎 の理由
        # （分類・コメント）を消す（苦情が良い評価として集計されないように。#97 レビュー）
        keep = {
            column
            for field, column in (("categories", "categories_json"), ("comment", "comment"))
            if field not in body.model_fields_set and body.thumb != "up"
        }
        return feedback_view(self.store.save_feedback(row, keep))

    async def send(self, session_id: str, text: str) -> AsyncIterator[SSEEvent]:
        state = self._state(session_id)
        self._activity[session_id] = time.monotonic()
        try:
            async for event in self._send(session_id, state, text):
                yield event
        finally:  # 切断で中断されても、ここまでの会話と成果物を残す
            self._activity[session_id] = time.monotonic()
            self.store.save_session(session_id, state)

    def interview_activity(self, idle_s: float) -> str | None:
        """インタビューの途中なら理由を返す（応答中か、直近 idle_s 秒に操作があった）。

        自動反映（#106）の dbt build の間はカタログを読めず、インタビューの tool が失敗するため。
        """
        if any(self.interviewer.is_busy(s) for s in list(self._states.values())):
            return "インタビューの応答の途中です"
        now = time.monotonic()
        if any(now - t < idle_s for t in list(self._activity.values())):
            return f"インタビューの途中です（直近 {idle_s / 60:g} 分に操作がありました）"
        return None

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
        if session_id in self._running():
            raise Conflict("生成ジョブを実行中です")
        self._check_not_syncing()
        try:
            beam = self.interviewer.finalize(state)
        except ValueError as e:
            raise Conflict(str(e)) from e
        self.store.save_session(session_id, state)
        self.store.add_version(session_id, beam)
        job = Job.new(session_id, STEPS)
        self._start(job, lambda on_step: self.pipeline(beam, on_step))
        return self.view(session_id)

    def _check_not_syncing(self) -> None:
        if SYNC_ID in self._running():
            raise Conflict("main の反映（dbt build）を実行中です。終わってから実行してください")

    def _running(self) -> set[str]:
        """実行中のジョブの session_id（メモリで動く。DB の running は起動時に失敗にする）。"""
        return {j.session_id for j in list(self._jobs.values()) if j.status == "running"}

    def _start(
        self,
        job: Job,
        run: Callable[[Callable[[StepUpdate], None]], Awaitable[PipelineResult]],
    ) -> None:
        self._jobs[job.id] = job
        self.store.save_job(job.id, job.session_id, job.status, job.steps)
        task = asyncio.get_running_loop().create_task(self._run_job(job, run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- 源泉の取り込み（#69） ----------------------------------------------------------------

    def start_onboarding(
        self, source: str, patch: OntologyPatch, cleansing: str, redo: bool = False
    ) -> dict[str, Any]:
        if self.onboarding is None:
            raise Conflict("源泉の取り込みは使えません")
        if ONBOARD_PREFIX + source in self._running():
            raise Conflict("この源泉の取り込みを実行中です")
        self._check_not_syncing()
        last = self._job_view(ONBOARD_PREFIX + source)
        if last and last["status"] == "succeeded" and not redo:
            # 同じ源泉の PR を 2 本作らない。前の PR を閉じた場合だけ、明示してやり直す
            raise Conflict(
                f"この源泉の取り込みの PR は作成済みです（{last['pr_url']}）。"
                "マージすればオントロジーに入ります。PR を閉じた場合は「やり直す」を選んでください"
            )
        job = Job.new(ONBOARD_PREFIX + source, ONBOARD_STEPS)
        runner = self.onboarding
        self._start(job, lambda on_step: runner(source, patch, cleansing, on_step))
        return job.view()

    def onboarding_job(self, source: str) -> dict[str, Any] | None:
        return self._job_view(ONBOARD_PREFIX + source)

    # --- マージ後の反映（#74） ------------------------------------------------------------------

    def start_sync(self) -> dict[str, Any]:
        if self.sync is None:
            raise Conflict("反映は使えません")
        running = self._running()
        if SYNC_ID in running:
            raise Conflict("反映を実行中です")
        # 反映の dbt build は共有の DuckDB に書く。生成・取り込みのジョブ（DB を写す）と重ねない
        if running - {SYNC_ID}:
            raise Conflict("生成・取り込みのジョブを実行中です。終わってから反映してください")
        job = Job.new(SYNC_ID, SYNC_STEPS)
        self._start(job, self.sync)
        return job.view()

    def sync_job(self) -> dict[str, Any] | None:
        return self._job_view(SYNC_ID)

    def reopen(self, session_id: str) -> dict[str, Any]:
        """確定を取り消して修正できるようにする。生成ジョブの実行中はできない。"""
        state = self._state(session_id)
        if session_id in self._running():
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

    async def _run_job(
        self,
        job: Job,
        run: Callable[[Callable[[StepUpdate], None]], Awaitable[PipelineResult]],
    ) -> None:
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
            result = await run(on_step)
            job.status = "succeeded" if result.ok else "failed"
            job.pr_url, job.error, job.branch = result.pr_url, result.error, result.branch
        except Exception as e:
            job.status, job.error = "failed", str(e)
        try:
            self.store.save_job(
                job.id, job.session_id, job.status, job.steps, job.pr_url, job.error, job.branch
            )
            # 保存できたら、以後は DB から読む（終わったジョブをメモリに溜めない）。購読中の
            # job_events はこの job を持っているので、下の done はそのまま届く
            self._jobs.pop(job.id, None)
        finally:
            self._publish(job, _sse("done", job.view()))
            for q in job.subscribers:
                q.put_nowait(None)

    def _publish(self, job: Job, event: SSEEvent) -> None:
        for q in job.subscribers:
            q.put_nowait(event)

    def _job_view(self, session_id: str) -> dict[str, Any] | None:
        return self._job_views([session_id]).get(session_id)

    def _job_views(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        """セッションごとの最新のジョブ。実行中はメモリ、終わったものは DB（1 回の問い合わせ）。"""
        # 一覧はスレッドで作るので、ループ側で増えても壊れないよう写しを見る
        live = {j.session_id: j.view() for j in list(self._jobs.values())}
        wanted = [sid for sid in session_ids if sid not in live]
        views = {sid: row.view() for sid, row in self.store.latest_jobs(wanted).items()}
        return views | {sid: live[sid] for sid in session_ids if sid in live}

    async def job_events(self, job_id: str) -> AsyncIterator[SSEEvent]:
        job = self._jobs.get(job_id)
        if job is None:
            row = self.store.job(job_id)
            if row is None:
                raise NotFound(f"ジョブ {job_id} がありません")
            yield _sse("done", row.view())
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
