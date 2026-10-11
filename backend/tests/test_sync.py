"""マージ後の自動反映（#74）: 手元が main で変更が無いときだけ取り込み、dbt build と検査をする。"""

import stat
from pathlib import Path

import pytest

from adp.pipeline import StepUpdate
from adp.sync import Sync
from adp.vcs import GitWorkspace
from tests.vcs.test_vcs import git


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(work, "config", k, v)
    (work / "warehouse").mkdir()
    (work / "backend").mkdir()
    (work / "warehouse" / "a.txt").write_text("1")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    # 別の場所で main を進める（GitHub で PR がマージされた状態）
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(other, "config", k, v)
    (other / "warehouse" / "b.txt").write_text("2")
    git(other, "add", ".")
    git(other, "commit", "-m", "merged pr")
    git(other, "push", "origin", "main")
    return work, remote


def _bin(tmp_path: Path, fail: str | None = None) -> Path:
    """dbt / mf / python の代わり（呼ばれた引数を記録し、fail を含む呼び出しだけ失敗する）。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    log = tmp_path / "calls.log"
    for name in ("dbt", "mf", "python"):
        path = bin_dir / name
        code = f'echo "{name} $*" >> {log}\n'
        if fail:
            code += f'case "$*" in *{fail}*) echo "boom"; exit 1;; esac\n'
        path.write_text("#!/bin/sh\n" + code)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_status_reports_behind_and_whether_it_can_pull(tmp_path: Path) -> None:
    work, _ = _repos(tmp_path)
    sync = Sync(work, _bin(tmp_path), GitWorkspace(work))
    status = sync.status()
    assert status.branch == "main" and status.clean and status.behind == 1 and status.can_pull

    (work / "warehouse" / "a.txt").write_text("edited")
    status = sync.status(fetch=False)
    assert not status.can_pull and status.reason and "変更" in status.reason

    git(work, "checkout", "-q", "--", ".")
    git(work, "switch", "-q", "-c", "feat/x")
    status = sync.status(fetch=False)
    assert not status.can_pull and status.reason and "main ではありません" in status.reason


async def test_run_pulls_builds_and_checks(tmp_path: Path) -> None:
    work, _ = _repos(tmp_path)
    updates: list[StepUpdate] = []
    result = await Sync(work, _bin(tmp_path), GitWorkspace(work)).run(updates.append)
    assert result.ok, result.error
    assert (work / "warehouse" / "b.txt").exists()  # 取り込んだ
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert calls[:3] == ["dbt deps", "dbt build", "dbt parse"]
    assert "python -m adp.ontology check" in calls and "python -m adp.cleansing check" in calls
    assert [u.status.value for u in updates if u.step == "pull"][-1] == "done"


async def test_run_does_not_pull_on_a_branch_and_stops_on_failure(tmp_path: Path) -> None:
    work, _ = _repos(tmp_path)
    git(work, "switch", "-q", "-c", "feat/x")
    result = await Sync(work, _bin(tmp_path), GitWorkspace(work)).run()
    assert not result.ok and result.error and "main ではありません" in result.error
    assert not (work / "warehouse" / "b.txt").exists()  # 作業中のブランチは触らない

    git(work, "switch", "-q", "main")
    result = await Sync(work, _bin(tmp_path / "x", fail="build"), GitWorkspace(work)).run()
    assert not result.ok and result.error and "build が失敗しました" in result.error


async def test_run_refuses_dirty_main_and_diverged_history(tmp_path: Path) -> None:
    work, _ = _repos(tmp_path)
    (work / "warehouse" / "a.txt").write_text("edited")
    result = await Sync(work, _bin(tmp_path), GitWorkspace(work)).run()
    assert not result.ok and result.error and "変更" in result.error
    assert not (tmp_path / "calls.log").exists()  # 共有の DB を作業中の内容で作り直さない

    git(work, "checkout", "-q", "--", ".")
    (work / "warehouse" / "c.txt").write_text("local")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "local")
    before = git(work, "rev-parse", "HEAD")
    result = await Sync(work, _bin(tmp_path / "y"), GitWorkspace(work)).run()
    assert not result.ok  # 早送りできない（手元にだけあるコミット）
    assert git(work, "rev-parse", "HEAD") == before


def test_app_code_changes_are_not_pulled_from_the_screen(tmp_path: Path) -> None:
    """アプリのコード（backend / frontend / Makefile）が変わる取り込みは画面からしない。"""
    work, _ = _repos(tmp_path)
    other = tmp_path / "other"
    (other / "backend").mkdir(exist_ok=True)
    (other / "backend" / "x.py").write_text("print(1)")
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "app change")
    git(other, "push", "-q", "origin", "main")
    status = Sync(work, _bin(tmp_path), GitWorkspace(work)).status()
    assert status.behind == 2 and not status.can_pull and status.can_rebuild
    assert status.reason and "backend/x.py" in status.reason and "make setup" in status.reason


def test_fetch_failure_is_reported_and_not_retried_immediately(tmp_path: Path) -> None:
    work, _ = _repos(tmp_path)
    git(work, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    sync = Sync(work, _bin(tmp_path), GitWorkspace(work))
    status = sync.status()
    assert status.fetch_error
    fetched_at = sync._cache.fetched_at
    sync.status()
    assert sync._cache.fetched_at == fetched_at  # すぐには繰り返さない


async def test_sync_is_exclusive_with_other_jobs(tmp_path: Path) -> None:
    import asyncio

    from fastapi.testclient import TestClient

    from adp.api.app import create_app
    from adp.api.service import Conflict, Job, SessionService
    from adp.api.store import Store
    from adp.interview import Interviewer
    from adp.llm import LLMSettings
    from adp.pipeline import PipelineResult

    release = asyncio.Event()

    async def slow(on_step):  # type: ignore[no-untyped-def]
        await release.wait()
        return PipelineResult(ok=True)

    async def pipeline(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    interviewer = Interviewer(None, LLMSettings())  # type: ignore[arg-type]
    service = SessionService(Store(tmp_path / "app.db"), interviewer, pipeline, sync=slow)
    service._jobs["g"] = Job(id="g", session_id="s1")  # 実行中の生成ジョブ
    with pytest.raises(Conflict, match="生成・取り込み"):
        service.start_sync()
    del service._jobs["g"]
    service.start_sync()
    with pytest.raises(Conflict, match="反映を実行中"):
        service.start_sync()
    with pytest.raises(Conflict, match="反映"):
        service._check_not_syncing()
    release.set()
    await asyncio.gather(*service._tasks)

    no_sync = SessionService(Store(tmp_path / "b.db"), interviewer, pipeline)
    client = TestClient(create_app(no_sync))
    assert client.get("/sync").status_code == 503
    assert client.post("/sync").status_code == 409


def test_auto_sync_starts_only_when_it_can_pull(tmp_path: Path) -> None:
    """マージがあって取り込めるときだけ反映を始め、できないときは理由を残す（#106）。"""
    from adp.sync import AutoSync, auto_interval

    work, _ = _repos(tmp_path)
    sync = Sync(work, _bin(tmp_path), GitWorkspace(work))
    started: list[int] = []
    auto = AutoSync(sync, lambda: started.append(1), interval_s=1)
    assert auto.tick() and started == [1] and auto.state.last_note is None

    def busy() -> None:
        raise RuntimeError("生成・取り込みのジョブを実行中です")

    assert not AutoSync(sync, busy, interval_s=1).tick()
    git(work, "checkout", "-q", "-b", "topic")
    blocked = AutoSync(sync, lambda: started.append(2), interval_s=1)
    assert not blocked.tick() and "main ではありません" in (blocked.state.last_note or "")
    assert started == [1]
    assert auto_interval({"ADP_AUTO_SYNC_INTERVAL_S": "0"}) == 0
    assert auto_interval({"ADP_AUTO_SYNC_INTERVAL_S": "x"}) == 60
    assert not AutoSync(sync, busy, interval_s=0).state.enabled


def test_merged_fact_appears_in_the_bus_matrix_automatically(tmp_path: Path) -> None:
    """生成 PR を main にマージすると、開いている画面の一覧（バスマトリクス）に fact が出る（#106）。

    origin で生成した fct を main に足し、アプリの自動反映が取り込んで dbt build するまで待つ。
    """
    import shutil
    import time

    from fastapi.testclient import TestClient

    from adp.api.app import create_app
    from adp.api.service import SessionService
    from adp.api.store import Store
    from adp.beam import load
    from adp.catalog.catalog import Catalog
    from adp.datasets import DatasetIndex
    from adp.design import DesignBuilder
    from adp.design.catalog_schema import CatalogSchema
    from adp.evals.repair import prepare_warehouse
    from adp.generation import generate_models
    from adp.generation.project import ProjectInfo
    from adp.interview import Interviewer
    from adp.llm import LLMSettings
    from adp.llm.settings import REPO_ROOT
    from adp.sync import AutoSync
    from tests.generation.test_dbt_codegen import DBT

    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    warehouse = prepare_warehouse(work)  # 生成した fct の無い warehouse と小さなサンプルデータ
    (work / "backend").mkdir()
    (work / ".gitignore").write_text(
        "warehouse/data/\nwarehouse/target/\nwarehouse/dbt_packages/\nwarehouse/logs/\n"
    )
    git(work, "init", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(work, "config", k, v)
    git(work, "add", ".")
    git(work, "commit", "-qm", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "-u", "origin", "main")

    # dbt は本物、検査（mf・python -m adp.*）は成功だけ返す偽物
    bin_dir = _bin(tmp_path)
    (bin_dir / "dbt").unlink()
    (bin_dir / "dbt").symlink_to(DBT)
    sync = Sync(work, bin_dir, GitWorkspace(work))
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    index = DatasetIndex(work, catalog)

    # GitHub で生成 PR がマージされた状態: 別の場所で fct を作って main に push する
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(other, "config", k, v)
    beam = load(REPO_ROOT / "backend" / "tests" / "fixtures" / "beam" / "sales_by_store.yml")
    info = ProjectInfo(warehouse, catalog)
    for f in generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam).files:
        (other / "warehouse" / f.path).parent.mkdir(parents=True, exist_ok=True)
        (other / "warehouse" / f.path).write_text(f.content, encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-qm", "merged generated pr")
    git(other, "push", "-q", "origin", "main")

    async def pipeline(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(
        Store(tmp_path / "app.db"),
        Interviewer(None, LLMSettings()),  # type: ignore[arg-type]
        pipeline,
        sync=sync.run,
    )
    auto = AutoSync(sync, service.start_sync, interval_s=0.2)
    app = create_app(service, index, sync=sync, auto_sync=auto)
    with TestClient(app) as client:
        before = {r["name"] for r in client.get("/datasets").json()["bus"]["rows"]}
        assert "fct_order_lines" not in before
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            job = client.get("/sync").json()["job"]
            if job and job["status"] != "running":
                break
            time.sleep(1)
        assert job and job["status"] == "succeeded", job
        after = {r["name"] for r in client.get("/datasets").json()["bus"]["rows"]}
        assert "fct_order_lines" in after
    shutil.rmtree(other)
