"""セッション・確定版・生成ジョブの永続化（SQLite、`.adp/app.db`）。#10"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Field, Session, SQLModel, create_engine, select

from adp.beam import BeamModel, to_yaml
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
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

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
    ) -> None:
        with Session(self.engine) as db:
            row = db.get(JobRow, job_id)
            if row is None:
                row = JobRow(id=job_id, session_id=session_id, status=status, steps_json="[]")
            row.status, row.pr_url, row.error = status, pr_url, error
            row.steps_json = json.dumps(steps, ensure_ascii=False)
            row.updated_at = _now()
            db.add(row)
            db.commit()

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
