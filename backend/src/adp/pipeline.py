"""確定した BEAM 成果物から PR を作るまでの生成ジョブ（design-doc の Scenario S1 の 7〜8）。

作業はすべてリポジトリ内の git worktree（`.adp/worktrees/<branch>`）で行い、開発中の
チェックアウトや DuckDB ファイルには触れない。検証に失敗して自己修復でも直らなければ、
draft PR にして人に渡す。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import duckdb
import yaml

from adp.beam import BeamModel, save
from adp.catalog.catalog import Catalog
from adp.charts import SPECS_DIR, ChartError, check, compile_board, render_png
from adp.design import DesignBuilder, PhysicalDesign
from adp.design.catalog_schema import CatalogSchema
from adp.design.check import existing_facts, load_ontology
from adp.generation import generate_models
from adp.generation.chart_specs import LINE_SERIES_MAX, chart_spec, source_of
from adp.generation.dbt import Generation
from adp.generation.project import ProjectInfo
from adp.generation.repair import Repairer, RepairResult
from adp.generation.semantic import MetricQuery
from adp.llm import AgentRunner, LLMSettings
from adp.ontology.models import Ontology, link_models
from adp.ontology.models import dump as dump_ontology
from adp.ontology.render import markdown as ontology_markdown
from adp.vcs import (
    ChartPreview,
    GitHub,
    GitWorkspace,
    Verification,
    Worktree,
    branch_name,
    pr_body,
    pr_title,
)
from adp.vcs.git import GIT_LOCK
from adp.warehouse import warehouse_db

T = TypeVar("T")

ONTOLOGY = Path("ontology") / "ontology.yml"
"""オントロジーの正本（リポジトリのルートから）。"""
ASSETS_DIR = "pr_assets"
"""PR 本文に載せる画像。commit して push した直後の commit で消す（main に残さない）。"""


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
    ("charts", "チャートを生成"),
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
    chart: ChartPreview | None = None


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


class JobSteps:
    """生成・取り込みのジョブで共通の、進捗の通知と worktree → commit → push の流れ。

    git・DB のコピー・dbt などの重い処理はスレッドで行い、イベントループ（ほかの要求や
    進捗の SSE）を止めない。push する前に失敗したら、作業ブランチを片付けて同じ要求で
    再実行できるようにする。git を変更する操作（worktree の追加・削除、stage・commit・画像の
    commit・push）は、すべてのジョブで共通の `GIT_LOCK` の中で行い、ジョブ同士で重ねない。
    dbt などの検証は lock の外で行うので、ジョブ同士で並行に動く。
    """

    def __init__(
        self,
        steps: list[tuple[str, str]],
        on_step: Callable[[StepUpdate], None],
        workspace: GitWorkspace,
        detail_limit: int | None = None,
    ) -> None:
        self.labels = dict(steps)
        self.on_step = on_step
        self.workspace = workspace
        self.detail_limit = detail_limit
        """失敗の詳細を画面に送るときの最大の長さ（None なら切らない）。"""
        self.current = ""
        self.worktree: Worktree | None = None
        self.pushed = False

    def step(self, name: str, status: StepStatus, detail: str = "") -> None:
        self.current = name
        self.on_step(StepUpdate(name, self.labels[name], status, detail))

    @staticmethod
    async def _git(fn: Callable[..., T], *args: Any) -> T:
        """git を変更する処理を、スレッドで GIT_LOCK を持って実行する。"""

        def locked() -> T:
            with GIT_LOCK:
                return fn(*args)

        return await asyncio.to_thread(locked)

    async def create_worktree(self, branch: str, base: str) -> Worktree:
        wt = await self._git(self.workspace.create_worktree, branch, base)
        self.worktree = wt
        return wt

    async def commit_and_push(
        self,
        wt: Worktree,
        paths: list[str],
        message: str,
        before_push: Callable[[], None] | None = None,
    ) -> list[str]:
        """paths を stage して commit し、push する。stage したファイルを返す。

        before_push は commit の後・push の前に（スレッドで）呼ぶ（PR 本文の画像の commit など）。
        """
        files = await self._git(self.workspace.stage, wt, paths)
        if not files:
            raise PipelineError("commit するファイルがありません")
        await self._git(self.workspace.commit, wt, message)
        if before_push is not None:
            await self._git(before_push)
        await self._git(self.workspace.push, wt)
        self.pushed = True
        return files

    async def fail(self, result: PipelineResult, error: Exception) -> PipelineResult:
        """失敗を結果と進捗に書き、push 前なら作業ブランチを片付ける。"""
        result.error = str(error)
        if self.current:
            detail = str(error)
            if self.detail_limit is not None:
                detail = detail[: self.detail_limit]
            self.step(self.current, StepStatus.FAILED, detail)
        if self.worktree is not None and not self.pushed:
            try:
                await self._git(self.workspace.remove_worktree, self.worktree, True)
                result.branch = None
            except Exception:
                pass
        return result


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
        job = JobSteps(STEPS, on_step, self.workspace)
        step = job.step
        thread = asyncio.to_thread
        result = PipelineResult(ok=False)
        try:
            if beam.finalized_at is None:
                raise PipelineError("BEAM 成果物が確定されていません")
            # push してから PR を作れないと判明しないよう、先に確かめる
            await thread(self.github.check)

            step("worktree", StepStatus.RUNNING)
            event = beam.events[0].name if beam.events else None
            wt = await job.create_worktree(branch_name(beam.name, event), self.config.base)
            result.branch = wt.branch
            warehouse = await thread(self._prepare_warehouse, wt)
            step("worktree", StepStatus.DONE, wt.branch)

            step("design", StepStatus.RUNNING)
            catalog = Catalog(warehouse_db(warehouse))
            ontology = await thread(load_ontology, wt.path / ONTOLOGY)
            facts_known = await thread(existing_facts, wt.path / "beam")
            builder = DesignBuilder(
                CatalogSchema(catalog), ontology=ontology, existing_facts=facts_known
            )
            design = await thread(builder.build, beam)
            result.design = design
            if design.blockers:
                raise PipelineError(
                    "設計で確認が必要な課題があります: "
                    + " / ".join(i.message for i in design.blockers)
                )
            step("design", StepStatus.DONE, f"fct {len(design.facts)} / dim {len(design.dims)}")

            step("generate", StepStatus.RUNNING)
            info = ProjectInfo(warehouse, catalog)
            # 生成の前からある fct（PR 本文に、再利用したか新しく作ったかを書く。#113）
            facts_before = sorted(m for m in info.existing_models() if m.startswith("fct_"))
            generation = await thread(self._generate, beam, design, ontology, wt, info)
            reused = f"、再利用 {len(generation.reused)} モデル" if generation.reused else ""
            step("generate", StepStatus.DONE, f"{len(generation.files)} ファイル{reused}")

            step("verify", StepStatus.RUNNING)
            repairer = Repairer(
                self.runner,
                self.settings,
                self.config.bin_dir,
                log=lambda msg: step("verify", StepStatus.RUNNING, msg),
            )
            repair = await repairer.run(warehouse, generation.queries, generation.tasks)
            result.repair = repair
            step(
                "verify",
                StepStatus.DONE if repair.ok else StepStatus.FAILED,
                repair.final.summary
                + (f"（自己修復 {len(repair.attempts)} 回）" if repair.attempts else ""),
            )

            step("charts", StepStatus.RUNNING)
            chart = await thread(self._charts, beam, generation.queries, wt)
            result.chart = chart
            if chart is None or chart.skipped:
                step(
                    "charts",
                    StepStatus.DONE,
                    (chart.skipped or "") if chart else "指標がありません",
                )
            else:
                step(
                    "charts",
                    StepStatus.FAILED if chart.error else StepStatus.DONE,
                    chart.error or f"{len(chart.charts)} チャート",
                )

            def commit_image() -> None:
                if chart is not None and not chart.error and not chart.skipped:
                    chart.image_url = self._commit_image(wt, beam)

            step("commit", StepStatus.RUNNING)
            files = await job.commit_and_push(
                wt,
                ["warehouse", "beam", "ontology", "docs/ontology.md"],
                # 件名も無害化する（squash の既定のメッセージになるため。#126）
                f"{pr_title(beam)}\n\nBEAM 成果物 beam/{beam.name}.yml から生成",
                before_push=commit_image,
            )
            result.files = files
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
            body = pr_body(beam, design, files, verification, generation.tasks, chart, facts_before)
            result.pr_url = await thread(
                self.github.create_pr, wt, pr_title(beam), body, draft=not repair.ok
            )
            step("pr", StepStatus.DONE, result.pr_url)
            result.ok = repair.ok
            if not repair.ok:
                result.error = "自己修復で直らなかったため、draft PR にして人に渡しました"
            return result
        except Exception as e:
            return await job.fail(result, e)

    @staticmethod
    def _generate(
        beam: BeamModel,
        design: PhysicalDesign,
        ontology: Ontology | None,
        wt: Worktree,
        info: ProjectInfo,
    ) -> Generation:
        """dbt・Semantic Layer・テストと BEAM 成果物を worktree に書く（ファイルと DB の I/O）。"""
        wt_root = wt.path
        ontology_path = wt_root / ONTOLOGY
        generation = generate_models(design, info, beam)
        info.write(generation.files)
        save(beam, wt_root / "beam" / f"{beam.name}.yml")
        if ontology is not None:
            # 作った fct / dim を、その源泉の概念に書き込む（fact / dim の整理。#69）
            links = [(f.base.table, f.name) for f in design.facts]
            links += [(d.base.table, d.name) for d in design.dims if not d.exists]
            ontology, linked = link_models(ontology, links)
            if linked:
                ontology_path.write_text(dump_ontology(ontology), encoding="utf-8")
                (wt_root / "docs" / "ontology.md").write_text(
                    ontology_markdown(ontology), encoding="utf-8"
                )
        return generation

    def _charts(
        self, beam: BeamModel, queries: list[MetricQuery], wt: Worktree
    ) -> ChartPreview | None:
        """既定のチャートの元ファイルとボードを書き、検査して PNG に描画する。

        失敗してもモデルと指標の PR は作る（チャートは含めず、理由を PR 本文に書く）。
        """
        warehouse = wt.path / "warehouse"
        info = ProjectInfo(warehouse, Catalog(warehouse_db(warehouse)))

        def cardinality(ref: str) -> int | None:
            column = source_of(beam, ref)
            if column is None:
                return None
            try:
                values = info.distinct_values(column[0], column[1], LINE_SERIES_MAX)
            except Exception:
                return None
            return LINE_SERIES_MAX + 1 if values is None else len(values)

        spec = chart_spec(beam, queries, cardinality)
        if spec is None:
            return None
        spec_rel = f"{SPECS_DIR}/{beam.name}.yml"
        spec_path = warehouse / spec_rel
        board_path = warehouse / "charts" / f"{beam.name}.yml"
        preview = ChartPreview(
            spec=f"warehouse/{spec_rel}",
            charts=[
                (c.get("title") or c.get("label") or name, c["type"])
                for name, c in spec["charts"].items()
            ],
        )
        if spec_path.exists() or board_path.exists():
            # 既存のチャート（手で整えたものを含む）は上書きも削除もしない
            preview.charts = []
            preview.skipped = f"既にチャートがあるため作りませんでした（warehouse/{spec_rel}）"
            return preview
        mf = self.config.bin_dir / "mf"

        def write(board: dict[str, Any]) -> None:
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            board_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(
                "# adp が BEAM の指標要求から生成（SQL は書かない。ADR 0002）\n"
                + yaml.safe_dump(board, allow_unicode=True, sort_keys=False, width=100),
                encoding="utf-8",
            )
            board_path.write_text(compile_board(beam.name, board, warehouse, mf), encoding="utf-8")
            problems = check(warehouse, mf)
            if problems:
                raise ChartError(" / ".join(problems))
            render_png(
                warehouse,
                self.config.bin_dir / "dct",
                beam.name,
                wt.path / ASSETS_DIR / f"{beam.name}.png",
            )

        def clean() -> None:
            spec_path.unlink(missing_ok=True)
            board_path.unlink(missing_ok=True)
            shutil.rmtree(wt.path / ASSETS_DIR, ignore_errors=True)

        try:
            write(spec)
            return preview
        except Exception as e:  # チャートの失敗で PR 全体を止めない
            clean()
            first_error = str(e)
        # 1 つの指標の失敗でボード全体を落とさない: 指標ごとに（書き出し・検査・描画まで）試し、
        # 描けるものだけで作り直す（#79）
        ok: list[str] = []
        failed: list[str] = []
        for name in spec["charts"]:
            try:
                write(_only(spec, [name]))
                ok.append(name)
            except Exception:
                failed.append(name)
            finally:
                clean()
        if not ok or not failed:
            preview.error = first_error
            return preview
        try:
            write(_only(spec, ok))
        except Exception as e:
            clean()
            preview.error = str(e)
            return preview
        titles = {n: c.get("title") or c.get("label") or n for n, c in spec["charts"].items()}
        preview.charts = [(titles[n], spec["charts"][n]["type"]) for n in ok]
        preview.omitted = [titles[n] for n in failed]
        return preview

    def _commit_image(self, wt: Worktree, beam: BeamModel) -> str | None:
        """画像を commit し、次の commit で消す。PR 本文は画像のある commit を参照する。"""
        image = f"{ASSETS_DIR}/{beam.name}.png"
        before: str | None = None
        try:
            repo = self.github.repo_name(wt)
            if repo is None or not (wt.path / image).exists():
                shutil.rmtree(wt.path / ASSETS_DIR, ignore_errors=True)
                return None
            before = self.workspace.git("rev-parse", "HEAD", cwd=wt.path)
            self.workspace.stage(wt, [ASSETS_DIR])
            sha = self.workspace.commit(
                wt, "chore: PR 本文に載せるチャートの画像（次の commit で削除）"
            )
            self.workspace.remove(wt, ASSETS_DIR)
            self.workspace.commit(wt, "chore: チャートの画像を削除（main には残さない）")
        except Exception:
            # 画像なしで PR を作る。画像の commit をブランチに残さない
            if before is not None:
                self.workspace.git("reset", "-q", "--hard", before, cwd=wt.path)
            shutil.rmtree(wt.path / ASSETS_DIR, ignore_errors=True)
            return None
        return f"https://github.com/{repo}/blob/{sha}/{image}?raw=true"

    def _prepare_warehouse(self, wt: Worktree) -> Path:
        return prepare_warehouse(self.config, wt)


def prepare_warehouse(config: PipelineConfig, wt: Worktree) -> Path:
    """worktree の warehouse に、検証用の DuckDB と dbt パッケージを用意する。"""
    warehouse = wt.path / "warehouse"
    (warehouse / "data").mkdir(parents=True, exist_ok=True)
    if not config.source_db.exists():
        raise PipelineError(f"検証用の DuckDB がありません: {config.source_db}")
    copy_database(config.source_db, warehouse_db(warehouse))
    packages = config.repo_root / "warehouse" / "dbt_packages"
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


def _only(spec: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """チャートの元ファイルのうち、names のチャート（とその問い合わせ）だけを残す。"""
    queries = {spec["charts"][n]["query"] for n in names}
    return {
        **spec,
        "metric_queries": {k: v for k, v in spec["metric_queries"].items() if k in queries},
        "charts": {n: spec["charts"][n] for n in names},
        "rows": [n for n in spec["rows"] if n in names],
    }
