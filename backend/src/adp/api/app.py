"""HTTP API（design-doc の Interfaces）。frontend からは /api 経由でプロキシされる。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, ValidationError
from sse_starlette.sse import EventSourceResponse

from adp.api.service import Conflict, NotFound, SessionService, SSEEvent
from adp.api.store import Store
from adp.catalog.catalog import CatalogUnavailable
from adp.chart_view import ChartRequest, ChartView, ChartViews
from adp.charts import ChartError
from adp.datasets import DatasetIndex
from adp.feedback import CATEGORIES, RETENTION_DAYS, FeedbackIn
from adp.onboarding.pipeline import (
    CLEANSING_MAX,
    AlreadyOnboarded,
    OnboardingPipeline,
    cleansing_text,
)
from adp.onboarding.proposal import OntologyPatch
from adp.sync import INTERVIEW_IDLE_S, AutoSync, Sync, auto_interval
from adp.warehouse import warehouse_db, warehouse_kind


class CreateSession(BaseModel):
    request: str = Field(min_length=1, max_length=2000)


class SendMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class OnboardingDraftIn(BaseModel):
    agent: bool = True
    """エージェントに案を作らせる（False ならプロファイルからの決定的な案だけ）。"""


class OnboardingIn(BaseModel):
    patch: dict[str, Any]
    cleansing: Annotated[str, Field(min_length=1, max_length=CLEANSING_MAX)] | dict[str, Any]
    """クレンジングの定義。YAML か、画面のフォームで直した JSON（#150）。"""
    redo: bool = False
    """前の取り込みの PR を閉じた（マージしなかった）ので、もう一度作る。"""


def create_app(
    service: SessionService,
    datasets: DatasetIndex | None = None,
    charts: ChartViews | None = None,
    onboarding: OnboardingPipeline | None = None,
    sync: Sync | None = None,
    auto_sync: AutoSync | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # マージされた PR を確かめて、取り込めるなら自動で反映する（#106）
        task = asyncio.create_task(auto_sync.loop()) if auto_sync else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="ADP", lifespan=lifespan)
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

    @app.get("/ontology")
    def ontology() -> dict[str, Any]:
        """オントロジー（概念・属性・関係・制約）の図と、源泉との食い違い（#67）。"""
        if datasets is None:
            raise HTTPException(503, "オントロジーは利用できません")
        try:
            return datasets.ontology().model_dump(mode="json")
        except FileNotFoundError as e:
            raise HTTPException(404, "ontology/ontology.yml がありません") from e
        except ValueError as e:  # yml の形式の誤り（pydantic の ValidationError を含む）
            raise HTTPException(422, f"ontology.yml の形式が正しくありません: {e}") from e

    @app.post("/sources/{table}/onboarding/draft")
    async def onboarding_draft(table: str, body: OnboardingDraftIn) -> dict[str, Any]:
        """源泉の取り込みの下書き（オントロジーへの追加案・クレンジングの定義・バスの差分。#69）。"""
        if onboarding is None:
            raise HTTPException(503, "源泉の取り込みは使えません")
        try:
            draft = await onboarding.draft(table, use_agent=body.agent)
        except AlreadyOnboarded as e:
            raise HTTPException(409, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(503, "ontology/ontology.yml がありません") from e
        except ValidationError as e:  # ontology.yml などの形式の誤り（入力の誤りではない）
            raise HTTPException(422, f"形式が正しくありません: {e}") from e
        except ValueError as e:  # 対象外のテーブル
            raise HTTPException(404, str(e)) from e
        except CatalogUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except duckdb.Error as e:
            raise HTTPException(503, "下書きを作れませんでした") from e
        return draft.model_dump(mode="json", by_alias=True)

    @app.post("/sources/{table}/onboarding")
    async def onboarding_start(table: str, body: OnboardingIn) -> dict[str, Any]:
        """確定した追加案とクレンジングの定義で、取り込みの PR を作るジョブを始める（#69）。"""
        if onboarding is None:
            raise HTTPException(503, "源泉の取り込みは使えません")
        try:
            patch = OntologyPatch.model_validate(body.patch)
            cleansing = cleansing_text(body.cleansing)
            errors = await asyncio.to_thread(onboarding.check_request, table, patch, cleansing)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        except CatalogUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(503, "ontology/ontology.yml がありません") from e
        except duckdb.Error as e:
            raise HTTPException(503, "検証できませんでした") from e
        if errors:
            raise HTTPException(422, " / ".join(errors))
        try:
            return service.start_onboarding(table, patch, cleansing, redo=body.redo)
        except Conflict as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/sources/{table}/onboarding")
    def onboarding_status(table: str) -> dict[str, Any]:
        """この源泉の最新の取り込みジョブ（無ければ job: null）。"""
        return {"job": service.onboarding_job(table)}

    @app.get("/sources/{table}/cleansing")
    def source_cleansing(table: str) -> dict[str, Any]:
        """源泉のクレンジングの定義と、プロファイルから作った雛形（#68）。"""
        if datasets is None:
            raise HTTPException(503, "データ一覧は利用できません")
        try:
            return datasets.cleansing(table).model_dump(mode="json")
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        except CatalogUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except duckdb.Error as e:  # DB の例外の本文は返さない（guardrails §3）
            raise HTTPException(503, "雛形を作れませんでした") from e

    @app.get("/sources/{table}/profile")
    def source_profile(table: str) -> dict[str, Any]:
        """源泉 1 つのデータプロファイル（列ごとの統計・キーの候補・使っているモデル。#62）。"""
        if datasets is None:
            raise HTTPException(503, "データ一覧は利用できません")
        try:
            return datasets.profile(table).model_dump(mode="json")
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        except CatalogUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except duckdb.Error as e:  # DB の例外の本文は返さない（guardrails §3）
            raise HTTPException(503, "プロファイルを計算できませんでした") from e

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

    @app.get("/sync")
    async def sync_status() -> dict[str, Any]:
        """手元と origin/main の差と、最新の反映ジョブ（#74）。"""
        if sync is None:
            raise HTTPException(503, "反映は使えません")
        try:
            status = await asyncio.to_thread(sync.status)
        except Exception as e:  # git が使えないなど
            raise HTTPException(503, f"状態を確かめられませんでした: {type(e).__name__}") from e
        return {
            "status": status.model_dump(mode="json"),
            "job": service.sync_job(),
            "auto": auto_sync.state.model_dump(mode="json") if auto_sync else None,
        }

    @app.post("/sync")
    async def sync_start() -> dict[str, Any]:
        """main の変更を取り込み、dbt build と検査をするジョブを始める（進捗は jobs の events）。"""
        try:
            return service.start_sync()
        except Conflict as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/sessions")
    async def list_sessions() -> dict[str, Any]:
        """これまでの要求と生成ジョブ・PR の状態の一覧（#73）。"""
        return await asyncio.to_thread(service.history)

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

    # 接続先（いまは DuckDB だけ。未対応の指定なら起動時に止める。#81）
    warehouse_kind(REPO_ROOT / ".env")
    settings = load_settings()
    runner = ClaudeAgentRunner(settings)
    db = warehouse_db(REPO_ROOT / "warehouse")
    catalog = Catalog(db, REPO_ROOT / "warehouse" / "target" / "manifest.json")
    interviewer = Interviewer(
        runner,
        settings,
        {SERVER_NAME: build_tools(catalog)},
        design_issues=design_issues(catalog, REPO_ROOT / "ontology" / "ontology.yml"),
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
    onboarding = OnboardingPipeline(pipeline.config, runner, settings)
    sync = Sync(REPO_ROOT, Path(sys.executable).parent, GitWorkspace(REPO_ROOT))
    service = SessionService(
        store,
        interviewer,
        pipeline.run,
        onboarding.run,
        pr_state=pipeline.github.pr_state,
        sync=sync.run,
    )
    auto = AutoSync(
        sync,
        service.start_sync,
        auto_interval(env),
        blocked=lambda: service.interview_activity(INTERVIEW_IDLE_S),
    )
    return create_app(
        service, DatasetIndex(REPO_ROOT, catalog), ChartViews(REPO_ROOT), onboarding, sync, auto
    )
