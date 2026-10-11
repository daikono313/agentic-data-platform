"""v3 の E2E（#69、certificate の「v3 の完了」の 2）。

新しい源泉（raw.inventory。dbt の sources に未宣言）を取り込み、オントロジーの更新 →
クレンジング層 →（PR をマージ）→ インタビュー → fact / dim と指標 → チャート、を
API で通しで動かす。

LLM は台本（取り込みの追加案と、インタビューの tool 呼び出し）を再生し、取り込みと生成のジョブは
本物（git worktree・dbt build・チャート・push）を一時リポジトリで動かす。PR 作成（gh）だけ偽物。
"""

import shutil
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from adp.api.app import create_app
from adp.api.service import SessionService
from adp.api.store import Store
from adp.beam import load as load_beam
from adp.catalog.catalog import Catalog
from adp.chart_view import ChartViews
from adp.datasets import DatasetIndex
from adp.design.check import design_issues
from adp.interview import Interviewer
from adp.llm import LLMSettings, ScriptedRunner
from adp.onboarding.pipeline import OnboardingPipeline
from adp.ontology import load as load_ontology
from adp.pipeline import GenerationPipeline, PipelineConfig
from adp.vcs import GitHub, GitWorkspace
from tests.api.test_api import parse_sse
from tests.generation.test_dbt_codegen import DBT, FIXTURES
from tests.generation.test_repair import FixingRunner
from tests.test_pipeline import FakeGh, repo  # noqa: F401  （fixture）
from tests.vcs.test_vcs import git

ROOT = Path(__file__).resolve().parents[2]

# エージェントが出す追加案（日本語の表示名・関係の動詞・制約）
PATCH: dict[str, Any] = {
    "domains": [{"name": "inventory", "label": "在庫"}],
    "concepts": [
        {
            "name": "inventory_movement",
            "label": "在庫の入出庫",
            "domain": "inventory",
            "kind": "event",
            "source": "raw.inventory",
            "key": "inventory_id",
            "attributes": [
                {"name": "moved_at", "label": "入出庫日時", "column": "moved_at"},
                {"name": "movement_type", "label": "入出庫の種類", "column": "movement_type"},
                {"name": "quantity", "label": "数量", "column": "quantity"},
            ],
        }
    ],
    "relations": [
        {
            "from": "store",
            "to": "inventory_movement",
            "verb": "入出庫する",
            "via": "raw.inventory.store_id",
        },
        {
            "from": "inventory_movement",
            "to": "product",
            "verb": "動かす",
            "via": "raw.inventory.product_id",
        },
    ],
    "constraints": [
        {"concept": "inventory_movement", "text": "数量は入荷がプラス、移動・廃棄がマイナス"}
    ],
}


def _wait(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        for name, data in parse_sse(client.get(f"/jobs/{job_id}/events").text):
            if name == "done":
                return data  # type: ignore[no-any-return]
        time.sleep(0.5)
    raise AssertionError("ジョブが終わりませんでした")


def test_v3_end_to_end(repo: Path) -> None:  # noqa: F811
    # 前提: オントロジーがある（main に入っている）
    (repo / "ontology").mkdir()
    shutil.copy(ROOT / "ontology" / "ontology.yml", repo / "ontology" / "ontology.yml")
    (repo / "docs").mkdir()
    shutil.copy(ROOT / "docs" / "ontology.md", repo / "docs" / "ontology.md")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "ontology")
    git(repo, "push", "origin", "main")

    db = repo / "warehouse/data/dev.duckdb"
    catalog = Catalog(db)
    gh = FakeGh()
    config = PipelineConfig(repo, DBT.parent, db, "daikono313")
    onboarding = OnboardingPipeline(
        config,
        ScriptedRunner([[("submit_proposal", {"patch": PATCH})]]),
        LLMSettings(),
        GitWorkspace(repo),
        GitHub(repo, "daikono313", gh),
    )
    beam = load_beam(FIXTURES / "beam" / "inventory_receipts.yml")
    beam_data = beam.model_dump(mode="json", exclude_none=True)
    turn: list[str | tuple[str, dict[str, Any]]] = [
        ("set_overview", {"name": beam.name, "title": beam.title}),
        ("upsert_event", {"data": beam_data["events"][0]}),
        ("upsert_metric", {"data": beam_data["metrics"][0]}),
        ("set_phase", {"phase": "review"}),
        "入荷数量を、月別・店舗別・商品カテゴリ別に見られるようにまとめました。",
    ]
    interviewer = Interviewer(
        ScriptedRunner([turn]),
        LLMSettings(),
        design_issues=design_issues(catalog, repo / "ontology" / "ontology.yml"),
    )
    pipeline = GenerationPipeline(
        config,
        FixingRunner(lambda n, spec: None),
        LLMSettings(),
        GitWorkspace(repo),
        GitHub(repo, "daikono313", gh),
    )
    service = SessionService(
        Store(repo / ".adp" / "app.db"), interviewer, pipeline.run, onboarding.run
    )
    # 1 つのイベントループでジョブを動かし続ける（with を使わないと、要求ごとにループが変わり、
    # スレッドに出した検証（asyncio.to_thread）の続きが動かない）
    app = create_app(service, DatasetIndex(repo, catalog), ChartViews(repo), onboarding)
    with TestClient(app) as client:
        # 1. 新しい源泉が「未整理」として見える
        sources = {s["name"]: s for s in client.get("/datasets").json()["sources"]}
        assert sources["raw.inventory"]["unmapped"] and not sources["raw.orders"]["unmapped"]

        # 2. 取り込みの下書き（エージェントの追加案・クレンジングの雛形・バスの候補）
        draft = client.post("/sources/raw.inventory/onboarding/draft", json={"agent": True}).json()
        assert draft["proposer"] == "agent", draft.get("note")
        assert draft["patch"]["concepts"][0]["label"] == "在庫の入出庫"
        assert "key: inventory_id" in draft["cleansing"]
        candidate = next(r for r in draft["bus"]["rows"] if r.get("candidate"))
        assert candidate["name"] == "fct_inventory_movement"

        # 3. 確定 → 取り込みの PR（オントロジー・クレンジングの定義・クレンジング層）
        cleansing = yaml.safe_load(draft["cleansing"])
        cleansing["exclude"] = [{"column": "movement_type", "equals": "test"}]
        started = client.post(
            "/sources/raw.inventory/onboarding",
            json={
                "patch": draft["patch"],
                "cleansing": yaml.safe_dump(cleansing, allow_unicode=True),
            },
        )
        assert started.status_code == 200, started.text
        done = _wait(client, started.json()["id"])
        assert done["status"] == "succeeded", (
            done["error"],
            [s for s in done["steps"] if s["status"] == "failed"],
        )
        branch = done["branch"]
        changed = git(repo, "diff", "--name-only", f"origin/main...origin/{branch}").splitlines()
        for path in (
            "ontology/ontology.yml",
            "docs/ontology.md",
            "warehouse/cleansing/raw.inventory.yml",  # exclude を足したので必ず変わる
            "warehouse/models/staging/stg_inventory.sql",
        ):
            assert path in changed, (path, changed)
        # 源泉の宣言と stg の yml は、この PR で作るか、すでに main にある（生成 PR が先に
        # 足した場合。#104）
        in_main = set(git(repo, "ls-tree", "-r", "--name-only", "origin/main").splitlines())
        for path in (
            "warehouse/models/staging/stg_inventory.yml",
            "warehouse/models/staging/_adp_inventory__sources.yml",
        ):
            assert path in changed or path in in_main, (path, changed)
        assert "源泉 `raw.inventory` を取り込みます" in gh.bodies[-1]

        # PR をマージする（本物ではオーナーが GitHub でマージする）
        git(repo, "fetch", "origin")
        git(repo, "merge", "--no-ff", "-m", "merge onboarding", f"origin/{branch}")
        git(repo, "push", "origin", "main")
        sources = {s["name"]: s for s in client.get("/datasets").json()["sources"]}
        assert not sources["raw.inventory"]["unmapped"]

        # 4. インタビュー → 確定 → 生成（fct / dim・指標・チャート）
        sid = client.post("/sessions", json={"request": beam.request}).json()["id"]
        events = parse_sse(
            client.post(f"/sessions/{sid}/messages", json={"text": "お願いします"}).text
        )
        assert events[-1][0] == "done"
        view = client.get(f"/sessions/{sid}").json()
        assert view["phase"] == "review" and view["issues"] == [], view["issues"]
        job_id = client.post(f"/sessions/{sid}/finalize").json()["job"]["id"]
        done = _wait(client, job_id)
        assert done["status"] == "succeeded", (
            done["error"],
            [s for s in done["steps"] if s["status"] == "failed"],
        )
        charts = next(s for s in done["steps"] if s["step"] == "charts")
        assert charts["status"] == "done", charts
        branch = done["branch"]
        changed = git(repo, "diff", "--name-only", f"origin/main...origin/{branch}").splitlines()
        assert "warehouse/models/marts/fct_inventory_movements.sql" in changed
        assert not any(
            p.startswith("warehouse/models/marts/dim_") for p in changed
        )  # 既存の dim を再利用

        # fact / dim の整理: 生成した fct が、源泉の概念に書き込まれる
        assert "ontology/ontology.yml" in changed and "docs/ontology.md" in changed
        git(repo, "fetch", "origin")
        text = git(repo, "show", f"origin/{branch}:ontology/ontology.yml")
        tmp = repo / ".adp" / "ontology.yml"
        tmp.write_text(text, encoding="utf-8")
        assert load_ontology(tmp).concept("inventory_movement").model == "fct_inventory_movements"

        # 5. 作った指標をチャートで見る（PR の内容でプレビュー）
        options = client.get("/charts/options", params={"job_id": job_id}).json()
        assert any(m["name"] == "received_quantity" for m in options["metrics"])
        rendered = client.post(
            "/charts/render",
            params={"job_id": job_id},
            json={
                "metric": "received_quantity",
                "dimension": "store__store_name",
                "grain": "month",
            },
        )
        assert rendered.status_code == 200, rendered.text
        assert rendered.json()["svg"].lstrip().startswith("<svg")
