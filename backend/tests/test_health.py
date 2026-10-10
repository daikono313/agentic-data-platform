from pathlib import Path

from fastapi.testclient import TestClient

from adp.api.app import create_app
from adp.api.service import SessionService
from adp.api.store import Store
from adp.interview import Interviewer
from adp.llm import LLMSettings, ScriptedRunner
from adp.pipeline import PipelineResult


async def _noop(beam, on_step):  # type: ignore[no-untyped-def]
    return PipelineResult(ok=True)


def test_health(tmp_path: Path) -> None:
    service = SessionService(
        Store(tmp_path / "a.db"), Interviewer(ScriptedRunner([]), LLMSettings()), _noop
    )
    client = TestClient(create_app(service))
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
