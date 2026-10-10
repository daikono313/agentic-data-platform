"""確定した BEAM 成果物から PR を作るまでの生成ジョブ（design-doc の Scenario S1 の 7〜8）。

作業はすべてリポジトリ内の git worktree（`.adp/worktrees/<branch>`）で行い、開発中の
チェックアウトや DuckDB ファイルには触れない。検証に失敗して自己修復でも直らなければ、
draft PR にして人に渡す。
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import duckdb

from adp.beam import BeamModel, save
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder, PhysicalDesign
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.project import ProjectInfo
from adp.generation.repair import Repairer, RepairResult
from adp.llm import AgentRunner, LLMSettings
from adp.vcs import GitHub, GitWorkspace, Verification, Worktree, branch_name, pr_body, pr_title


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


STEPS: list[tuple[str, str]] = [
    ("worktree", "作業ブランチを作成"),
    ("design", "スター設計"),
    ("generate", "dbt・Semantic Layer・テストを生成"),
    ("verify", "テスト（失敗時は自己修復）"),
    ("commit", "commit / push"),
    ("pr", "PR を作成"),
]


@dataclass
class StepUpdate:
    step: str
    label: str
    status: StepStatus
    detail: str = ""


@dataclass
class PipelineResult:
    ok: bool
    pr_url: str | None = None
    branch: str | None = None
    error: str | None = None
    design: PhysicalDesign | None = None
    repair: RepairResult | None = None
    files: list[str] = field(default_factory=list)


class PipelineError(Exception):
    pass


@dataclass
class PipelineConfig:
    repo_root: Path
    bin_dir: Path
    """dbt / mf のあるディレクトリ。"""
    source_db: Path
    """検証に使う DuckDB（worktree にコピーして使う）。"""
    github_account: str | None
    base: str = "origin/main"


class GenerationPipeline:
    def __init__(
        self,
        config: PipelineConfig,
        runner: AgentRunner,
        settings: LLMSettings,
        workspace: GitWorkspace | None = None,
        github: GitHub | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.settings = settings
        self.workspace = workspace or GitWorkspace(config.repo_root)
        self.github = github or GitHub(config.repo_root, config.github_account)

    async def run(
        self, beam: BeamModel, on_step: Callable[[StepUpdate], None] = lambda _: None
    ) -> PipelineResult:
        labels = dict(STEPS)
        current = ""

        def step(name: str, status: StepStatus, detail: str = "") -> None:
            nonlocal current
            current = name
            on_step(StepUpdate(name, labels[name], status, detail))

        result = PipelineResult(ok=False)
        worktree: Worktree | None = None
        pushed = False
        try:
            if beam.finalized_at is None:
                raise PipelineError("BEAM 成果物が確定されていません")
            # push してから PR を作れないと判明しないよう、先に確かめる
            self.github.check()

            step("worktree", StepStatus.RUNNING)
            event = beam.events[0].name if beam.events else None
            wt = self.workspace.create_worktree(branch_name(beam.name, event), self.config.base)
            worktree = wt
            result.branch = wt.branch
            warehouse = self._prepare_warehouse(wt)
            step("worktree", StepStatus.DONE, wt.branch)

            step("design", StepStatus.RUNNING)
            catalog = Catalog(warehouse / "data" / "dev.duckdb")
            design = DesignBuilder(CatalogSchema(catalog)).build(beam)
            result.design = design
            if design.blockers:
                raise PipelineError(
                    "設計で確認が必要な課題があります: "
                    + " / ".join(i.message for i in design.blockers)
                )
            step("design", StepStatus.DONE, f"fct {len(design.facts)} / dim {len(design.dims)}")

            step("generate", StepStatus.RUNNING)
            info = ProjectInfo(warehouse, catalog)
            generation = generate_models(design, info, beam)
            info.write(generation.files)
            save(beam, wt.path / "beam" / f"{beam.name}.yml")
            reused = f"、再利用 {len(generation.reused)} モデル" if generation.reused else ""
            step("generate", StepStatus.DONE, f"{len(generation.files)} ファイル{reused}")

            step("verify", StepStatus.RUNNING)
            repairer = Repairer(
                self.runner,
                self.settings,
                self.config.bin_dir,
                log=lambda msg: on_step(
                    StepUpdate("verify", labels["verify"], StepStatus.RUNNING, msg)
                ),
            )
            repair = await repairer.run(warehouse, generation.queries, generation.tasks)
            result.repair = repair
            step(
                "verify",
                StepStatus.DONE if repair.ok else StepStatus.FAILED,
                repair.final.summary
                + (f"（自己修復 {len(repair.attempts)} 回）" if repair.attempts else ""),
            )

            step("commit", StepStatus.RUNNING)
            files = self.workspace.stage(wt, ["warehouse", "beam"])
            if not files:
                raise PipelineError("commit するファイルがありません")
            result.files = files
            self.workspace.commit(
                wt, f"feat(beam): {beam.title}\n\nBEAM 成果物 beam/{beam.name}.yml から生成"
            )
            self.workspace.push(wt)
            pushed = True
            step("commit", StepStatus.DONE, f"{len(files)} ファイル")

            step("pr", StepStatus.RUNNING)
            verification = Verification(
                dbt_build_passed=repair.ok,
                summary=repair.final.summary,
                queries=[
                    (f"{q.metric} by {','.join(q.group_by) or '-'}", repair.ok)
                    for q in generation.queries
                ],
                repair_attempts=[
                    f"{a.model}: {'成功' if a.result.ok else '失敗'}"
                    + (f" — {a.agent_summary[:200]}" if a.agent_summary else "")
                    for a in repair.attempts
                ],
            )
            body = pr_body(beam, design, files, verification, generation.tasks)
            result.pr_url = self.github.create_pr(wt, pr_title(beam), body, draft=not repair.ok)
            step("pr", StepStatus.DONE, result.pr_url)
            result.ok = repair.ok
            if not repair.ok:
                result.error = "自己修復で直らなかったため、draft PR にして人に渡しました"
            return result
        except Exception as e:
            result.error = str(e)
            if current:
                step(current, StepStatus.FAILED, str(e))
            # push 前の失敗なら作業ブランチを片付けて、同じ BEAM で再実行できるようにする
            if worktree is not None and not pushed:
                try:
                    self.workspace.remove_worktree(worktree, delete_branch=True)
                    result.branch = None
                except Exception:
                    pass
            return result

    def _prepare_warehouse(self, wt: Worktree) -> Path:
        """worktree の warehouse に、検証用の DuckDB と dbt パッケージを用意する。"""
        warehouse = wt.path / "warehouse"
        (warehouse / "data").mkdir(parents=True, exist_ok=True)
        if not self.config.source_db.exists():
            raise PipelineError(f"検証用の DuckDB がありません: {self.config.source_db}")
        copy_database(self.config.source_db, warehouse / "data" / "dev.duckdb")
        packages = self.config.repo_root / "warehouse" / "dbt_packages"
        if packages.exists() and not (warehouse / "dbt_packages").exists():
            shutil.copytree(packages, warehouse / "dbt_packages")
        return warehouse


def copy_database(source: Path, dest: Path) -> None:
    """DuckDB を、WAL（未チェックポイントの変更）も含めて整合した状態でコピーする。

    ファイルを直接コピーすると、書き込み中の変更（.wal）が抜けることがある。
    書き込み中でロックされていれば、わかりやすいエラーにする。
    """
    if dest.exists():
        dest.unlink()
    con = duckdb.connect()
    try:
        con.execute(f"attach '{source}' as src (read_only)")
        con.execute(f"attach '{dest}' as dst")
        con.execute("copy from database src to dst")
    except duckdb.IOException as e:
        raise PipelineError(
            f"検証用の DuckDB を読めません（dbt などが書き込み中の可能性があります）: {source}"
        ) from e
    finally:
        con.close()
