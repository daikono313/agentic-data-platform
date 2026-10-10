"""v2 の E2E（#46、design-doc の S3 と S4 の 1〜2）。

データ一覧 → インタビュー中のフィードバック → 確定 → PR（チャート定義と画像を含む）→ 描画画面
（PR の内容でプレビュー）→ フィードバックの集計、を API で通しで動かす。

LLM は記録した実際のインタビュー（tests/fixtures/evals/return_rate_by_category.json）の
tool 呼び出しを再生し、生成ジョブは本物（dbt build・自己修復の検証・チャート・git worktree・
push）を一時リポジトリで動かす。PR 作成（gh）だけ偽物に置き換える。
"""

import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from adp.api.app import create_app
from adp.api.service import SessionService
from adp.api.store import Store
from adp.catalog.catalog import Catalog
from adp.chart_view import ChartViews
from adp.datasets import DatasetIndex
from adp.design.check import design_issues
from adp.evals.feedback import report
from adp.interview import Interviewer
from adp.llm import LLMSettings, ScriptedRunner
from adp.pipeline import GenerationPipeline, PipelineConfig
from adp.vcs import GitHub, GitWorkspace
from tests.api.test_api import parse_sse
from tests.generation.test_dbt_codegen import DBT
from tests.generation.test_repair import FixingRunner
from tests.test_pipeline import FakeGh, repo  # noqa: F401  （fixture）
from tests.vcs.test_vcs import git

RECORD = Path(__file__).parent / "fixtures" / "evals" / "return_rate_by_category.json"


def test_v2_end_to_end(repo: Path) -> None:  # noqa: F811
    record: dict[str, Any] = json.loads(RECORD.read_text(encoding="utf-8"))
    turns: list[list[Any]] = [
        [
            (c["name"].removeprefix("mcp__beam__"), c["input"])
            for c in turn
            if c["name"].startswith("mcp__beam__")
        ]
        for turn in record["tool_calls"]
    ]
    # 発言の本文も入れる（フィードバックを付ける対象）
    turns[0] = [*turns[0], "返品率の粒度（1 行が何を表すか）を決めましょう。"]
    catalog = Catalog(repo / "warehouse/data/dev.duckdb")
    # 本番と同じく、確定前に設計の課題も確かめる
    interviewer = Interviewer(
        ScriptedRunner(turns), LLMSettings(), design_issues=design_issues(catalog)
    )
    gh = FakeGh()
    pipeline = GenerationPipeline(
        PipelineConfig(repo, DBT.parent, repo / "warehouse/data/dev.duckdb", "daikono313"),
        FixingRunner(lambda n, spec: None),
        LLMSettings(),
        GitWorkspace(repo),
        GitHub(repo, "daikono313", gh),
    )
    store = Store(repo / ".adp" / "app.db")
    service = SessionService(store, interviewer, pipeline.run)
    client = TestClient(create_app(service, DatasetIndex(repo, catalog), ChartViews(repo)))

    # S3-1. 開始画面で使えるデータを見る（返品のテーブルがある）
    datasets = client.get("/datasets").json()
    assert any(s["name"] == "raw.returns" for s in datasets["sources"]), datasets["errors"]

    # S3-3. 要求を出してインタビュー（記録した各ターンを再生）
    request = record["beam"]["request"]
    sid = client.post("/sessions", json={"request": request}).json()["id"]
    for i in range(len(turns)):
        events = parse_sse(
            client.post(f"/sessions/{sid}/messages", json={"text": f"回答 {i}"}).text
        )
        assert events[-1][0] == "done"
        if i == 0:
            # S4-1. インタビュー中に、わかりにくい質問へフィードバックする
            res = client.post(
                f"/sessions/{sid}/feedback",
                json={
                    "kind": "message",
                    "message_index": 1,
                    "thumb": "down",
                    "categories": ["jargon"],
                    "comment": "『粒度』の意味がわからない",
                },
            )
            assert res.status_code == 200, res.text
            assert res.json()["prompt_version"]
    view = client.get(f"/sessions/{sid}").json()
    assert view["phase"] == "review" and view["issues"] == [], view["issues"]

    # S3-4. 確定 → 生成ジョブ → PR（チャートの定義と画像を含む）
    final = client.post(f"/sessions/{sid}/finalize").json()
    job_id = final["job"]["id"]
    done = None
    deadline = time.monotonic() + 600
    while done is None and time.monotonic() < deadline:
        for name, data in parse_sse(client.get(f"/jobs/{job_id}/events").text):
            if name == "done":
                done = data
    assert done is not None, "生成ジョブが終わりませんでした"
    assert done["status"] == "succeeded", done
    charts = next(s for s in done["steps"] if s["step"] == "charts")
    assert charts["status"] == "done", charts
    branch = done["branch"]
    added = git(repo, "diff", "--name-only", f"origin/main...origin/{branch}").splitlines()
    beam_name = record["beam"]["name"]
    assert f"warehouse/chart_specs/{beam_name}.yml" in added
    assert f"warehouse/charts/{beam_name}.yml" in added
    assert not any(f.startswith("pr_assets/") for f in added)  # 画像は main に残さない
    body = gh.bodies[-1]
    assert "## チャート" in body and "![チャート](https://github.com/" in body

    # S4-1. セッションの最後に全体の評価
    res = client.post(
        f"/sessions/{sid}/feedback", json={"kind": "session", "score": 3, "comment": "質問が多い"}
    )
    assert res.status_code == 200, res.text

    # S3-4. 完了画面から、作った指標の描画画面へ（PR の内容でプレビュー）
    metric = record["beam"]["metrics"][0]["name"]
    options = client.get("/charts/options", params={"job_id": job_id}).json()
    assert options["preview"] is True
    info = next(m for m in options["metrics"] if m["name"] == metric)
    dimension = next(d["name"] for d in info["dimensions"] if d["kind"] == "categorical")
    for req in (
        {"metric": metric, "grain": "month"},
        {"metric": metric, "dimension": dimension, "grain": "week"},
        {"metric": metric, "dimension": dimension, "grain": None, "start": "2024-01-01"},
    ):
        rendered = client.post("/charts/render", params={"job_id": job_id}, json=req)
        assert rendered.status_code == 200, rendered.text
        assert rendered.json()["svg"].lstrip().startswith("<svg")

    # S4-2. フィードバックの集計に、インタビュー中の内容が出る
    text = report(store.feedback())
    assert "👎 1" in text and "『粒度』の意味がわからない" in text
    assert "| 専門用語がわからない | 1 | `no_unexplained_jargon` |" in text
    assert "平均 3.0 点" in text
