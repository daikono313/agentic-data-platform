"""セッション・確定版・生成ジョブの永続化（SQLite、`.adp/app.db`）。#10"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, text
from sqlalchemy import select as sa_select
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

from adp.beam import BeamModel, to_yaml
from adp.feedback import RETENTION_DAYS
from adp.interview import InterviewState
from adp.interview.state import Phase


def _now() -> datetime:
    return datetime.now(UTC)


class SessionRow(SQLModel, table=True):
    __tablename__ = "sessions"
    id: str = Field(primary_key=True)
    state_json: str
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class BeamVersionRow(SQLModel, table=True):
    """確定した BEAM 成果物の版。確定し直すたびに増える。"""

    __tablename__ = "beam_versions"
    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    version: int
    beam_yaml: str
    created_at: datetime = Field(default_factory=_now)


class JobRow(SQLModel, table=True):
    __tablename__ = "jobs"
    id: str = Field(primary_key=True)
    session_id: str = Field(index=True)
    status: str
    steps_json: str
    pr_url: str | None = None
    error: str | None = None
    branch: str | None = None
    """生成ジョブの作業ブランチ（描画画面で PR の内容をプレビューするため。#43）。"""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def view(self) -> dict[str, Any]:
        return job_view(
            self.id, self.status, json.loads(self.steps_json), self.pr_url, self.error, self.branch
        )


def job_view(
    id: str,
    status: str,
    steps: list[dict[str, Any]],
    pr_url: str | None,
    error: str | None,
    branch: str | None,
) -> dict[str, Any]:
    """画面に返すジョブの形（実行中の service.Job も、保存した JobRow もこれで作る）。"""
    return {
        "id": id,
        "status": status,
        "steps": steps,
        "pr_url": pr_url,
        "error": error,
        "branch": branch,
    }


@dataclass(frozen=True)
class SessionSummary:
    """一覧（#73）に出すセッションの要約。読めない行は phase が None。"""

    id: str
    updated_at: datetime
    title: str
    request: str
    phase: str | None
    metrics: list[str]


_PHASES = {p.value for p in Phase}


def _labels(metrics_json: Any) -> list[str]:
    try:
        metrics = json.loads(metrics_json) if isinstance(metrics_json, str) else []
    except ValueError:
        return []
    if not isinstance(metrics, list):
        return []
    return [m["label"] for m in metrics if isinstance(m, dict) and isinstance(m.get("label"), str)]


class FeedbackRow(SQLModel, table=True):
    """インタビューへのフィードバック（#44）。保持期限は feedback.RETENTION_DAYS（O7）。"""

    __tablename__ = "feedback"
    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    kind: str
    """message / session"""
    message_index: int | None = None
    thumb: str | None = None
    score: int | None = None
    categories_json: str = "[]"
    comment: str = ""
    phase: str | None = None
    """その発言を作ったときの段階（session の場合は評価したときの段階）。"""
    question: str | None = None
    choices_json: str = "[]"
    message_text: str = ""
    """評価した発言（抜粋）。"""
    user_text: str = ""
    """その直前のユーザーの発言（抜粋）。"""
    prompt_version: str | None = None
    model: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Store:
    def __init__(self, path: Path, feedback_retention_days: int = RETENTION_DAYS) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self._add_missing_columns()
        self._feedback_lock = threading.Lock()
        self.prune_feedback(feedback_retention_days)

    def _add_missing_columns(self) -> None:
        """後から足した列を、既存の DB にも追加する（create_all は既存のテーブルを変えない）。"""
        with self.engine.begin() as conn:
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(jobs)")}
            if "branch" not in columns:
                conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN branch VARCHAR")

    # --- session ---
    def save_session(self, session_id: str, state: InterviewState) -> None:
        with Session(self.engine) as db:
            row = db.get(SessionRow, session_id)
            data = state.model_dump_json()
            if row is None:
                db.add(SessionRow(id=session_id, state_json=data))
            else:
                row.state_json = data
                row.updated_at = _now()
                db.add(row)
            db.commit()

    def session_summaries(self, limit: int = 200) -> list[SessionSummary]:
        """一覧に出す項目だけを、状態の JSON を検証せずに SQLite の JSON 関数で取り出す。

        新しく更新された順。JSON として読めない・beam が無い・段階が今の値でない行は phase を
        None にする（それ以外の項目の検証はしない。開くときに検証する）。
        """
        state = col(SessionRow.state_json)

        def extract(path: str, fn: Any = func.json_extract) -> Any:
            # 壊れた JSON に JSON 関数を使うとクエリ全体が失敗するので、先に確かめる
            return case((func.json_valid(state) == 1, fn(state, path)), else_=None)

        stmt = (
            sa_select(
                col(SessionRow.id),
                col(SessionRow.updated_at),
                extract("$.beam", func.json_type),
                extract("$.phase"),
                extract("$.beam.title"),
                extract("$.beam.request"),
                extract("$.beam.metrics"),
            )
            .order_by(col(SessionRow.updated_at).desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:  # 列が多く sqlmodel の select の型が合わない
            rows = conn.execute(stmt).all()
        result = []
        for id_, updated_at, beam_type, phase, title, request, metrics in rows:
            if beam_type != "object" or phase not in _PHASES:
                result.append(SessionSummary(id_, updated_at, "", "", None, []))
                continue
            result.append(
                SessionSummary(
                    id_,
                    updated_at,
                    title if isinstance(title, str) else "",
                    request if isinstance(request, str) else "",
                    phase,
                    _labels(metrics),
                )
            )
        return result

    def load_session(self, session_id: str) -> InterviewState | None:
        with Session(self.engine) as db:
            row = db.get(SessionRow, session_id)
            return InterviewState.model_validate_json(row.state_json) if row else None

    # --- beam versions ---
    def add_version(self, session_id: str, beam: BeamModel) -> int:
        with Session(self.engine) as db:
            versions = db.exec(
                select(BeamVersionRow).where(BeamVersionRow.session_id == session_id)
            ).all()
            version = len(versions) + 1
            db.add(BeamVersionRow(session_id=session_id, version=version, beam_yaml=to_yaml(beam)))
            db.commit()
            return version

    def versions(self, session_id: str) -> list[BeamVersionRow]:
        with Session(self.engine) as db:
            return list(
                db.exec(
                    select(BeamVersionRow)
                    .where(BeamVersionRow.session_id == session_id)
                    .order_by(BeamVersionRow.version)  # type: ignore[arg-type]
                ).all()
            )

    # --- job ---
    def save_job(
        self,
        job_id: str,
        session_id: str,
        status: str,
        steps: list[dict[str, Any]],
        pr_url: str | None = None,
        error: str | None = None,
        branch: str | None = None,
    ) -> None:
        with Session(self.engine) as db:
            row = db.get(JobRow, job_id)
            if row is None:
                row = JobRow(id=job_id, session_id=session_id, status=status, steps_json="[]")
            row.status, row.pr_url, row.error = status, pr_url, error
            row.branch = branch or row.branch
            row.steps_json = json.dumps(steps, ensure_ascii=False)
            row.updated_at = _now()
            db.add(row)
            db.commit()

    def fail_stale_jobs(self) -> int:
        """前回の起動で実行中のまま終わったジョブを失敗にする（ジョブはメモリ上で動くため）。"""
        with Session(self.engine) as db:
            rows = db.exec(select(JobRow).where(JobRow.status == "running")).all()
            for row in rows:
                row.status = "failed"
                row.error = (
                    "サーバーの再起動で中断されました。もう一度実行してください"
                    if row.session_id.startswith(("sync:", "onboard:"))
                    else "サーバーの再起動で中断されました。再開して確定し直してください"
                )
                db.add(row)
            db.commit()
            return len(rows)

    def latest_jobs(self, session_ids: list[str]) -> dict[str, JobRow]:
        """セッションごとの最新のジョブを 1 回の問い合わせで（一覧。#110）。"""
        if not session_ids:
            return {}
        with Session(self.engine) as db:
            rows = db.exec(
                select(JobRow)
                .where(col(JobRow.session_id).in_(session_ids))
                .order_by(col(JobRow.created_at), text("jobs.rowid"))
            ).all()
        return {row.session_id: row for row in rows}  # 後の（新しい）行で上書きする

    def job(self, job_id: str) -> JobRow | None:
        with Session(self.engine) as db:
            return db.get(JobRow, job_id)

    # --- feedback（#44） ---
    def save_feedback(self, row: FeedbackRow, keep: set[str] | None = None) -> FeedbackRow:
        """同じ発言（セッション全体の評価はセッションごとに 1 つ）への送り直しは上書きする。

        keep に挙げた項目は、既存の値を残す（👍 / 👎 だけを送り直してもコメントを消さない）。
        同時に送られても 1 件にまとめるため、保存はロックの中で行う。
        """
        with self._feedback_lock, Session(self.engine) as db:
            existing = db.exec(
                select(FeedbackRow).where(
                    FeedbackRow.session_id == row.session_id,
                    FeedbackRow.kind == row.kind,
                    FeedbackRow.message_index == row.message_index
                    if row.message_index is not None
                    else FeedbackRow.message_index.is_(None),  # type: ignore[union-attr]
                )
            ).first()
            if existing is not None:
                data = row.model_dump(exclude={"id", "created_at", *(keep or set())})
                for key, value in data.items():
                    setattr(existing, key, value)
                existing.updated_at = _now()
                row = existing
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def feedback(self, session_id: str | None = None) -> list[FeedbackRow]:
        with Session(self.engine) as db:
            query = select(FeedbackRow)
            if session_id is not None:
                query = query.where(FeedbackRow.session_id == session_id)
            return list(db.exec(query.order_by(FeedbackRow.id)).all())  # type: ignore[arg-type]

    def prune_feedback(self, days: int) -> int:
        """保持期限（O7）を過ぎたフィードバックを削除する。0 以下なら削除しない。"""
        if days <= 0:
            return 0
        cutoff = _now() - timedelta(days=days)
        with Session(self.engine) as db:
            rows = db.exec(select(FeedbackRow).where(FeedbackRow.updated_at < cutoff)).all()
            for row in rows:
                db.delete(row)
            db.commit()
            return len(rows)
