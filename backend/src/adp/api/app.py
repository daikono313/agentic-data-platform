"""HTTP API（design-doc の Interfaces）。frontend からは /api 経由でプロキシされる。"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from adp.api.service import Conflict, NotFound, SessionService, SSEEvent
from adp.api.store import Store


class CreateSession(BaseModel):
    request: str = Field(min_length=1, max_length=2000)


class SendMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


def create_app(service: SessionService) -> FastAPI:
    app = FastAPI(title="ADP")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sessions")
    def create_session(body: CreateSession) -> dict[str, Any]:
        return service.view(service.create(body.request))

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        try:
            return service.view(session_id)
        except NotFound as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: SendMessage) -> EventSourceResponse:
        try:
            service.view(session_id)
        except NotFound as e:
            raise HTTPException(404, str(e)) from e

        async def stream() -> AsyncIterator[SSEEvent]:
            try:
                async for event in service.send(session_id, body.text):
                    yield event
            except Conflict as e:
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {"message": {"role": "assistant", "text": ""}, "error": str(e)},
                        ensure_ascii=False,
                    ),
                }

        return EventSourceResponse(stream())

    @app.post("/sessions/{session_id}/finalize")
    async def finalize(session_id: str) -> dict[str, Any]:
        try:
            return service.finalize(session_id)
        except NotFound as e:
            raise HTTPException(404, str(e)) from e
        except Conflict as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/sessions/{session_id}/reopen")
    async def reopen(session_id: str) -> dict[str, Any]:
        try:
            return service.reopen(session_id)
        except NotFound as e:
            raise HTTPException(404, str(e)) from e
        except Conflict as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str) -> EventSourceResponse:
        if not service.has_job(job_id):
            raise HTTPException(404, f"ジョブ {job_id} がありません")
        return EventSourceResponse(service.job_events(job_id))

    return app


def build_default_app() -> FastAPI:
    """本番の組み立て（実際の Claude、ソースカタログ、生成パイプライン）。"""
    from adp.catalog.catalog import Catalog
    from adp.catalog.tools import SERVER_NAME, build_tools
    from adp.interview import Interviewer
    from adp.llm import ClaudeAgentRunner, load_settings
    from adp.llm.settings import REPO_ROOT, read_dotenv
    from adp.pipeline import GenerationPipeline, PipelineConfig
    from adp.vcs import GitWorkspace

    settings = load_settings()
    runner = ClaudeAgentRunner(settings)
    db = REPO_ROOT / "warehouse" / "data" / "dev.duckdb"
    catalog = Catalog(db, REPO_ROOT / "warehouse" / "target" / "manifest.json")
    interviewer = Interviewer(runner, settings, {SERVER_NAME: build_tools(catalog)})
    account = (
        read_dotenv(REPO_ROOT / ".env").get("ADP_GITHUB_ACCOUNT")
        or GitWorkspace(REPO_ROOT).remote_owner()
    )
    pipeline = GenerationPipeline(
        PipelineConfig(
            repo_root=REPO_ROOT,
            bin_dir=Path(sys.executable).parent,
            source_db=db,
            github_account=account,
        ),
        runner,
        settings,
    )
    service = SessionService(Store(REPO_ROOT / ".adp" / "app.db"), interviewer, pipeline.run)
    return create_app(service)
