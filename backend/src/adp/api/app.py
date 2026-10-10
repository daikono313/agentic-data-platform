"""HTTP API（design-doc の Interfaces）。frontend からは /api 経由でプロキシされる。"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from adp.api.service import Conflict, NotFound, SessionService, SSEEvent
from adp.api.store import Store
from adp.chart_view import ChartRequest, ChartView, ChartViews
from adp.charts import ChartError
from adp.datasets import DatasetIndex
from adp.feedback import CATEGORIES, RETENTION_DAYS, FeedbackIn


class CreateSession(BaseModel):
    request: str = Field(min_length=1, max_length=2000)


class SendMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


def create_app(
    service: SessionService,
    datasets: DatasetIndex | None = None,
    charts: ChartViews | None = None,
) -> FastAPI:
    app = FastAPI(title="ADP")
    # DNS リバインディング対策: ローカル以外の Host からの要求を拒否する（認証の無い API のため）
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "testserver"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/datasets")
    def list_datasets() -> dict[str, Any]:
        """取得可能な指標・モデル・元データ・BEAM 成果物の一覧（#38）。"""
        if datasets is None:
            raise HTTPException(503, "データ一覧は利用できません")
        return datasets.all().model_dump(mode="json")

    def _chart_view(job_id: str | None) -> ChartView:
        if charts is None:
            raise HTTPException(503, "描画画面は使えません")
        if job_id is None:
            return charts.main()
        branch = service.job_branch(job_id)
        if branch is None:
            raise HTTPException(404, f"ジョブ {job_id} の PR がありません")
        try:
            return charts.for_branch(branch)
        except ChartError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/charts/options")
    def chart_options(job_id: str | None = None) -> dict[str, Any]:
        """描画できる指標と切り口。job_id を渡すと、その生成 PR の内容（マージ前）で見る。"""
        metrics, errors = _chart_view(job_id).metrics()
        return {
            "metrics": [m.model_dump(mode="json") for m in metrics],
            "errors": errors,
            "preview": job_id is not None,
        }

    @app.post("/charts/render")
    def chart_render(req: ChartRequest, job_id: str | None = None) -> dict[str, Any]:
        try:
            return _chart_view(job_id).render(req).model_dump(mode="json")
        except ChartError as e:
            raise HTTPException(422, str(e)) from e

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

    @app.post("/sessions/{session_id}/feedback")
    def send_feedback(session_id: str, body: FeedbackIn) -> dict[str, Any]:
        """発言ごとの評価・コメント、セッション全体の評価（#44）。"""
        try:
            return service.feedback(session_id, body)
        except NotFound as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/feedback/categories")
    def feedback_categories() -> dict[str, str]:
        return CATEGORIES

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
    from adp.design.check import design_issues
    from adp.interview import Interviewer
    from adp.llm import ClaudeAgentRunner, load_settings
    from adp.llm.settings import REPO_ROOT, read_dotenv
    from adp.pipeline import GenerationPipeline, PipelineConfig
    from adp.vcs import GitWorkspace

    settings = load_settings()
    runner = ClaudeAgentRunner(settings)
    db = REPO_ROOT / "warehouse" / "data" / "dev.duckdb"
    catalog = Catalog(db, REPO_ROOT / "warehouse" / "target" / "manifest.json")
    interviewer = Interviewer(
        runner,
        settings,
        {SERVER_NAME: build_tools(catalog)},
        design_issues=design_issues(catalog),
    )
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
    env = read_dotenv(REPO_ROOT / ".env")
    retention = env.get("ADP_FEEDBACK_RETENTION_DAYS") or os.environ.get(
        "ADP_FEEDBACK_RETENTION_DAYS"
    )
    try:
        retention_days = int(retention) if retention else RETENTION_DAYS
    except ValueError:
        print(
            f"ADP_FEEDBACK_RETENTION_DAYS は日数（整数）で指定してください: {retention!r}。"
            f"{RETENTION_DAYS} 日にします",
            file=sys.stderr,
        )
        retention_days = RETENTION_DAYS
    store = Store(REPO_ROOT / ".adp" / "app.db", feedback_retention_days=retention_days)
    service = SessionService(store, interviewer, pipeline.run)
    return create_app(service, DatasetIndex(REPO_ROOT, catalog), ChartViews(REPO_ROOT))
