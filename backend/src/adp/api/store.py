"""セッション・確定版・生成ジョブの永続化（SQLite、`.adp/app.db`）。#10"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Field, Session, SQLModel, col, create_engine, select

from adp.beam import BeamModel, to_yaml
from adp.feedback import RETENTION_DAYS
from adp.interview import InterviewState


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

    def list_sessions(self, limit: int = 200) -> list[SessionRow]:
        """新しく更新された順（一覧画面。#73）。"""
        with Session(self.engine) as db:
            stmt = select(SessionRow).order_by(col(SessionRow.updated_at).desc()).limit(limit)
            return list(db.exec(stmt))

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
                row.error = "サーバーの再起動で中断されました。再開して確定し直してください"
                db.add(row)
            db.commit()
            return len(rows)

    def latest_job(self, session_id: str) -> JobRow | None:
        with Session(self.engine) as db:
            return db.exec(
                select(JobRow)
                .where(JobRow.session_id == session_id)
                .order_by(JobRow.created_at.desc())  # type: ignore[attr-defined]
            ).first()

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
