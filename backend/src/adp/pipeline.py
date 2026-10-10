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
        try:
            if beam.finalized_at is None:
                raise PipelineError("BEAM 成果物が確定されていません")

            step("worktree", StepStatus.RUNNING)
            event = beam.events[0].name if beam.events else None
            wt = self.workspace.create_worktree(branch_name(beam.name, event), self.config.base)
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
            files = self._changed_files(wt)
            result.files = files
            self.workspace.commit(
                wt, files, f"feat(beam): {beam.title}\n\nBEAM 成果物 beam/{beam.name}.yml から生成"
            )
            self.workspace.push(wt)
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
            return result

    def _prepare_warehouse(self, wt: Worktree) -> Path:
        """worktree の warehouse に、検証用の DuckDB と dbt パッケージを用意する。"""
        warehouse = wt.path / "warehouse"
        (warehouse / "data").mkdir(parents=True, exist_ok=True)
        if not self.config.source_db.exists():
            raise PipelineError(f"検証用の DuckDB がありません: {self.config.source_db}")
        shutil.copy2(self.config.source_db, warehouse / "data" / "dev.duckdb")
        packages = self.config.repo_root / "warehouse" / "dbt_packages"
        if packages.exists() and not (warehouse / "dbt_packages").exists():
            shutil.copytree(packages, warehouse / "dbt_packages")
        return warehouse

    def _changed_files(self, wt: Worktree) -> list[str]:
        """生成・修正されたファイル（warehouse/ と beam/ のうち git で追跡されうるもの）。"""
        out = self.workspace.git("status", "--porcelain", "--untracked-files=all", cwd=wt.path)
        files = []
        for line in out.splitlines():
            path = line[3:].strip()
            if path.startswith(("warehouse/", "beam/")):
                files.append(path)
        if not files:
            raise PipelineError("commit するファイルがありません")
        return sorted(files)
