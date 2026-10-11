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
        if list(args)[:3] == ["gh", "auth", "token"]:
            out = "tok"
        elif list(args)[:3] == ["gh", "repo", "view"]:
            out = "daikono313/repo\n"
        else:
            out = "https://github.com/o/r/pull/1\n"
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
        "charts",
        "commit",
        "pr",
    ]
    # 生成物と BEAM 成果物が commit・push され、DuckDB などは含まれない
    pushed = git(repo, "ls-tree", "-r", "--name-only", f"origin/{result.branch}")
    assert "beam/sales_by_store.yml" in pushed
    assert "warehouse/models/marts/fct_order_lines.sql" in pushed
    assert "warehouse/tests/assert_fct_order_lines_reconciles_with_source.sql" in pushed
    assert "dev.duckdb" not in pushed
    # チャート（#42）: 元ファイルとボードは残し、PR 本文の画像は main に残さない
    assert "warehouse/chart_specs/sales_by_store.yml" in pushed
    assert "warehouse/charts/sales_by_store.yml" in pushed
    assert "pr_assets" not in pushed
    assert result.chart is not None and result.chart.error is None, result.chart
    assert result.chart.charts == [("売上金額", "line")]
    url = result.chart.image_url
    assert url is not None and url.startswith("https://github.com/daikono313/repo/blob/")
    sha = url.split("/blob/")[1].split("/")[0]
    image = subprocess.run(
        ["git", "show", f"{sha}:pr_assets/sales_by_store.png"], cwd=repo, capture_output=True
    ).stdout
    assert image.startswith(b"\x89PNG")
    create = next(c for c in gh.calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" not in create
    body = gh.bodies[-1]
    assert "## BEAM Canvas" in body and "✅ 成功" in body
    assert f"![チャート]({url})" in body


async def test_chart_failure_still_creates_pr(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """チャートを描画できなくても、モデルと指標の PR は作る（チャートは含めず理由を書く）。"""
    from adp import pipeline as module
    from adp.charts import ChartError

    def broken(*_: object) -> None:
        raise ChartError("描画できませんでした")

    monkeypatch.setattr(module, "render_png", broken)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    beam.finalize()
    gh = FakeGh()
    config = PipelineConfig(
        repo_root=repo,
        bin_dir=DBT.parent,
        source_db=repo / "warehouse" / "data" / "dev.duckdb",
        github_account="daikono313",
    )
    pipeline = GenerationPipeline(
        config,
        FixingRunner(lambda n, spec: None),
        LLMSettings(),
        GitWorkspace(repo),
        GitHub(repo, "daikono313", gh),
    )
    result = await pipeline.run(beam)
    assert result.ok, result.error
    assert result.chart is not None and result.chart.error == "描画できませんでした"
    pushed = git(repo, "ls-tree", "-r", "--name-only", f"origin/{result.branch}")
    assert "chart_specs/sales_by_store.yml" not in pushed and "charts/sales_by_store" not in pushed
    assert "❌ チャートを作れませんでした" in gh.bodies[-1]


async def test_existing_chart_is_not_overwritten(repo: Path) -> None:
    """同じ BEAM の再実行で、既存（手で整えたものを含む）のチャートを上書き・削除しない。"""
    spec = repo / "warehouse" / "chart_specs" / "sales_by_store.yml"
    spec.parent.mkdir(exist_ok=True)
    spec.write_text("# 手で整えたチャート\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "existing chart")
    git(repo, "push", "origin", "main")
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    beam.finalize()
    gh = FakeGh()
    config = PipelineConfig(
        repo_root=repo,
        bin_dir=DBT.parent,
        source_db=repo / "warehouse" / "data" / "dev.duckdb",
        github_account="daikono313",
    )
    pipeline = GenerationPipeline(
        config,
        FixingRunner(lambda n, spec: None),
        LLMSettings(),
        GitWorkspace(repo),
        GitHub(repo, "daikono313", gh),
    )
    result = await pipeline.run(beam)
    assert result.ok, result.error
    assert result.chart is not None and result.chart.skipped
    assert not any("chart" in f for f in result.files), result.files
    shown = git(repo, "show", f"origin/{result.branch}:warehouse/chart_specs/sales_by_store.yml")
    assert shown.strip() == "# 手で整えたチャート"
    assert "既にチャートがある" in gh.bodies[-1]


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


def test_one_failing_chart_does_not_drop_the_board(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """1 つの指標のチャートが作れなくても、ほかの指標のチャートでボードを作る（#79）。"""
    import adp.pipeline as pipeline_module
    from adp.charts import ChartError
    from adp.generation.semantic import MetricQuery
    from adp.vcs import Worktree

    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    second = beam.metrics[0].model_copy(update={"name": "broken", "label": "壊れた指標"})
    beam = beam.model_copy(update={"metrics": [beam.metrics[0], second]})
    compiled: list[list[str]] = []

    def compile_board(name, spec, warehouse, mf):  # type: ignore[no-untyped-def]
        compiled.append(list(spec["charts"]))
        if "broken" in spec["charts"]:
            raise ChartError("broken を描けません")
        return "board"

    monkeypatch.setattr(pipeline_module, "compile_board", compile_board)
    monkeypatch.setattr(pipeline_module, "check", lambda warehouse, mf: [])
    monkeypatch.setattr(pipeline_module, "render_png", lambda *a: None)
    (tmp_path / "warehouse").mkdir()
    pipe = GenerationPipeline(
        PipelineConfig(tmp_path, tmp_path, tmp_path / "x.duckdb", "daikono313"),
        FixingRunner(lambda n, spec: None),
        LLMSettings(),
    )
    queries = [
        MetricQuery("revenue", ["store__channel"]),
        MetricQuery("broken", ["store__channel"]),
    ]
    preview = pipe._charts(beam, queries, Worktree(tmp_path, "beam/x"))
    assert preview is not None
    assert [t for t, _ in preview.charts] == ["売上金額"]
    assert preview.error and "壊れた指標" in preview.error
    assert compiled[-1] == ["revenue"]  # 描けるものだけで作り直した
    spec = (tmp_path / "warehouse" / "chart_specs" / f"{beam.name}.yml").read_text()
    assert "broken" not in spec
