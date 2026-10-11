"""E2E（design-doc S1、#17）: API でインタビューし、確定して、生成ジョブが PR を作るまで。

LLM は記録した実際のインタビュー（tests/fixtures/evals/sales_by_store.json）の tool 呼び出しを
再生し、生成ジョブは本物（dbt build・自己修復の検証・git worktree・push）を一時リポジトリで動かす。
PR 作成（gh）だけ偽物に置き換える。
"""

import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from adp.api.app import create_app
from adp.api.service import SessionService
from adp.api.store import Store
from adp.interview import Interviewer
from adp.llm import LLMSettings, ScriptedRunner
from adp.pipeline import GenerationPipeline, PipelineConfig
from adp.vcs import GitHub, GitWorkspace
from tests.api.test_api import parse_sse
from tests.generation.test_dbt_codegen import DBT
from tests.generation.test_repair import FixingRunner
from tests.test_pipeline import FakeGh, repo  # noqa: F401  （fixture）
from tests.vcs.test_vcs import git

RECORD = Path(__file__).parent / "fixtures" / "evals" / "sales_by_store.json"


def test_s1_end_to_end(repo: Path) -> None:  # noqa: F811
    record: dict[str, Any] = json.loads(RECORD.read_text(encoding="utf-8"))
    turns = [
        [
            (c["name"].removeprefix("mcp__beam__"), c["input"])
            for c in turn
            if c["name"].startswith("mcp__beam__")
        ]
        for turn in record["tool_calls"]
    ]
    interviewer = Interviewer(ScriptedRunner(turns), LLMSettings())  # type: ignore[arg-type]
    gh = FakeGh()
    pipeline = GenerationPipeline(
        PipelineConfig(repo, DBT.parent, repo / "warehouse/data/dev.duckdb", "daikono313"),
        FixingRunner(lambda n, spec: None),
        LLMSettings(),
        GitWorkspace(repo),
        GitHub(repo, "daikono313", gh),
    )
    service = SessionService(Store(repo / ".adp" / "app.db"), interviewer, pipeline.run)
    # 生成ジョブはバックグラウンドのタスクで進むので、イベントループを保ったまま使う
    with TestClient(create_app(service)) as client:
        # 1. 要求を入力してセッションを始める
        request = record["beam"]["request"]
        sid = client.post("/sessions", json={"request": request}).json()["id"]

        # 2〜6. インタビュー（記録した各ターンを再生）
        for i in range(len(turns)):
            events = parse_sse(
                client.post(f"/sessions/{sid}/messages", json={"text": f"回答 {i}"}).text
            )
            assert events[-1][0] == "done"
        view = client.get(f"/sessions/{sid}").json()
        assert view["phase"] == "review" and view["issues"] == []
        assert view["matrix"]["rows"]

        # 6〜8. 確定 → 生成ジョブ → PR
        final = client.post(f"/sessions/{sid}/finalize").json()
        job_id = final["job"]["id"]
        done = None
        deadline = time.monotonic() + 300
        while done is None and time.monotonic() < deadline:
            for name, data in parse_sse(client.get(f"/jobs/{job_id}/events").text):
                if name == "done":
                    done = data
        assert done is not None, "生成ジョブが終わりませんでした"
        assert done["status"] == "succeeded", done
        assert done["pr_url"] == "https://github.com/o/r/pull/1"
        assert all(s["status"] == "done" for s in done["steps"])

        # PR のブランチで追加: BEAM 成果物・fct の SQL と YAML（Semantic Layer）・突合テスト
        branch = git(repo, "branch", "-r", "--list", "origin/beam/*").strip()
        added = git(repo, "diff", "--name-only", f"origin/main...{branch}").splitlines()
        fct = next(
            f for f in added if f.startswith("warehouse/models/marts/fct_") and f.endswith(".sql")
        )
        fct_yml = fct.removesuffix(".sql") + ".yml"
        assert f"beam/{record['beam']['name']}.yml" in added
        assert fct_yml in added
        assert any(f.startswith("warehouse/tests/assert_") for f in added)
        yml = git(repo, "show", f"{branch}:{fct_yml}")
        assert "semantic_model:" in yml and "metrics:" in yml and "unit_tests:" in yml
        body = gh.bodies[-1]
        assert request in body and "✅ 成功" in body and "erDiagram" in body
        # S1 の 8: ソースに無いデータ（年収など）が未解決事項として PR に載る
        assert "## 未解決事項" in body and "data_unavailable" in body
