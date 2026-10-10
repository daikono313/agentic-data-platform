"""生成ジョブを、一時的な git リポジトリ（warehouse の複製 + bare リモート）で通しで実行する。"""

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from adp.beam import load
from adp.llm import LLMSettings
from adp.pipeline import GenerationPipeline, PipelineConfig, StepStatus, StepUpdate
from adp.vcs import GitHub, GitWorkspace
from tests.generation.test_dbt_codegen import DBT, FIXTURES, ROOT, _warehouse_copy
from tests.generation.test_repair import FixingRunner
from tests.vcs.test_vcs import git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote" / "daikono313" / "repo.git"
    remote.mkdir(parents=True)
    git(remote, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(work, "config", k, v)
    wh = _warehouse_copy(work)  # work/warehouse（data と dbt_packages を含む）
    shutil.copy(ROOT / ".gitignore", work / ".gitignore")
    (work / "beam").mkdir()
    (work / "beam" / ".gitkeep").write_text("")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    assert (wh / "data" / "dev.duckdb").exists()
    return work


class FakeGh:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []

    def __call__(
        self, args: Sequence[str], cwd: Path, env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if "--body-file" in args:
            self.bodies.append(Path(list(args)[list(args).index("--body-file") + 1]).read_text())
        out = (
            "tok"
            if list(args)[:3] == ["gh", "auth", "token"]
            else "https://github.com/o/r/pull/1\n"
        )
        return subprocess.CompletedProcess(args, 0, out, "")


async def test_pipeline_end_to_end(repo: Path) -> None:
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    beam.finalize()
    gh = FakeGh()
    config = PipelineConfig(
        repo_root=repo,
        bin_dir=DBT.parent,
        source_db=repo / "warehouse" / "data" / "dev.duckdb",
        github_account="daikono313",
    )
    updates: list[StepUpdate] = []
    pipeline = GenerationPipeline(
        config,
        FixingRunner(lambda n, spec: None),
        LLMSettings(),
        GitWorkspace(repo),
        GitHub(repo, "daikono313", gh),
    )
    result = await pipeline.run(beam, updates.append)
    assert result.ok, result.error
    assert result.pr_url == "https://github.com/o/r/pull/1"
    assert [u.step for u in updates if u.status == StepStatus.DONE] == [
        "worktree",
        "design",
        "generate",
        "verify",
        "commit",
        "pr",
    ]
    # 生成物と BEAM 成果物が commit・push され、DuckDB などは含まれない
    pushed = git(repo, "ls-tree", "-r", "--name-only", f"origin/{result.branch}")
    assert "beam/sales_by_store.yml" in pushed
    assert "warehouse/models/marts/fct_order_lines.sql" in pushed
    assert "warehouse/tests/assert_fct_order_lines_reconciles_with_source.sql" in pushed
    assert "dev.duckdb" not in pushed
    create = next(c for c in gh.calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" not in create
    body = gh.bodies[-1]
    assert "## BEAM Canvas" in body and "✅ 成功" in body


async def test_pipeline_rejects_unfinalized_beam(repo: Path) -> None:
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    config = PipelineConfig(repo, DBT.parent, repo / "warehouse/data/dev.duckdb", "daikono313")
    updates: list[StepUpdate] = []
    result = await GenerationPipeline(config, FixingRunner(lambda n, s: None), LLMSettings()).run(
        beam, updates.append
    )
    assert not result.ok and "確定されていません" in (result.error or "")


async def test_failed_run_cleans_up_and_can_rerun(repo: Path) -> None:
    """設計の段階で止まっても作業ブランチが残らず、同じ BEAM で再実行できる。"""
    from adp.beam import SourceRef

    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    beam.finalize()
    broken = beam.model_copy(deep=True)
    broken.events[0].dimensions[0].fact_key = SourceRef(table="raw.unknown", column="x")
    config = PipelineConfig(repo, DBT.parent, repo / "warehouse/data/dev.duckdb", "daikono313")
    gh = GitHub(repo, "daikono313", FakeGh())
    pipeline = GenerationPipeline(
        config, FixingRunner(lambda n, s: None), LLMSettings(), GitWorkspace(repo), gh
    )
    failed = await pipeline.run(broken)
    assert not failed.ok and "設計で確認が必要" in (failed.error or "")
    assert git(repo, "branch", "--list", "beam/*") == ""
    assert not any((repo / ".adp" / "worktrees").iterdir())
    ok = await pipeline.run(beam)
    assert ok.ok and ok.branch and ok.branch.endswith("-20261010") is not None


async def test_missing_account_stops_before_any_work(repo: Path) -> None:
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    beam.finalize()
    config = PipelineConfig(repo, DBT.parent, repo / "warehouse/data/dev.duckdb", None)
    result = await GenerationPipeline(config, FixingRunner(lambda n, s: None), LLMSettings()).run(
        beam
    )
    assert not result.ok and "アカウント" in (result.error or "")
    assert git(repo, "branch", "--list", "beam/*") == ""


def test_copy_database_includes_wal(tmp_path: Path) -> None:
    import duckdb

    from adp.pipeline import copy_database

    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("create table t as select 1 as x")
    con.execute("checkpoint")
    con.execute("pragma disable_checkpoint_on_shutdown")
    con.execute("insert into t values (2)")
    con.close()
    dest = tmp_path / "dest.duckdb"
    copy_database(src, dest)
    row = duckdb.connect(str(dest), read_only=True).execute("select count(*) from t").fetchone()
    assert row == (2,)
