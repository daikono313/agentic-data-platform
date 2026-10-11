"""源泉の取り込み: 下書きを作り、確定したら PR にする（#69）。

確定の内容（PR 1 本）:
- `ontology/ontology.yml`（追加案を適用）と `docs/ontology.md`
- `warehouse/cleansing/<schema>.<table>.yml`（クレンジングの定義）
- クレンジング層 `warehouse/models/staging/stg_<table>.sql|yml`
  （定義から生成。未宣言の源泉は sources も）
検証は `dbt build` / `dbt parse` と、オントロジー・クレンジングの検査。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path

import yaml

from adp.catalog.catalog import Catalog
from adp.cleansing.check import draft as cleansing_draft
from adp.cleansing.check import source_columns, validate
from adp.cleansing.models import CleansingSpec, dump_spec, spec_path
from adp.datasets import DatasetIndex
from adp.generation.dbt import staging_files
from adp.generation.project import ProjectInfo
from adp.generation.repair import verify
from adp.llm import AgentRunner, LLMSettings
from adp.onboarding.agent import propose_with_agent
from adp.onboarding.plan import OnboardingDraft, bus_with_candidates
from adp.onboarding.proposal import OntologyPatch, apply, propose_from_profile, validate_patch
from adp.ontology.check import check as check_ontology
from adp.ontology.models import dump, load
from adp.ontology.render import markdown, mermaid
from adp.pipeline import (
    PipelineConfig,
    PipelineError,
    PipelineResult,
    StepStatus,
    StepUpdate,
    prepare_warehouse,
)
from adp.vcs import GitHub, GitWorkspace, Worktree

ONBOARD_STEPS: list[tuple[str, str]] = [
    ("worktree", "作業ブランチを作成"),
    ("write", "オントロジーとクレンジングの定義を書く"),
    ("verify", "クレンジング層を検証（dbt build）"),
    ("commit", "commit / push"),
    ("pr", "PR を作成"),
]
ONTOLOGY = Path("ontology") / "ontology.yml"


def onboarding_branch(source: str, today: date | None = None) -> str:
    stem = re.sub(r"[^a-z0-9_-]", "-", source.rpartition(".")[2].lower())
    return f"onboard/{stem}-{(today or date.today()).strftime('%Y%m%d')}"


def parse_cleansing(text: str, source: str) -> CleansingSpec:
    """画面から受け取ったクレンジングの定義（YAML）を読む。源泉が違えば ValueError。"""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"クレンジングの定義を YAML として読めません: {e}") from e
    spec = CleansingSpec.model_validate(data)
    if spec.source != source:
        raise ValueError(f"クレンジングの定義の source（{spec.source}）が {source} と違います")
    return spec


class OnboardingPipeline:
    def __init__(
        self,
        config: PipelineConfig,
        runner: AgentRunner | None,
        settings: LLMSettings,
        workspace: GitWorkspace | None = None,
        github: GitHub | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.settings = settings
        self.workspace = workspace or GitWorkspace(config.repo_root)
        self.github = github or GitHub(config.repo_root, config.github_account)

    # --- 下書き ------------------------------------------------------------------------------

    async def draft(self, source: str, use_agent: bool = True) -> OnboardingDraft:
        """追加案（エージェント → 失敗したらプロファイルから）・クレンジングの定義・バスの差分。"""
        repo = self.config.repo_root
        catalog = Catalog(self.config.source_db, repo / "warehouse" / "target" / "manifest.json")
        ontology = load(repo / ONTOLOGY)
        if any(c.source == source for c in ontology.concepts):
            raise ValueError(f"{source} は既にオントロジーにあります")
        profile = await asyncio.to_thread(catalog.profile_table, source)
        source = f"{profile.schema_name}.{profile.name}"
        patch: OntologyPatch | None = None
        proposer, note = "profile", None
        if use_agent and self.runner is not None:
            result = await propose_with_agent(self.runner, self.settings, ontology, catalog, source)
            if result.patch is not None:
                patch, proposer = result.patch, "agent"
            else:
                note = (
                    "エージェントの案を使えなかったため、プロファイルから作りました"
                    f"（{result.error}）"
                )
        if patch is None:
            patch = propose_from_profile(profile, ontology)
        path = spec_path(repo / "warehouse" / "cleansing", source)
        exists = path.exists()
        cleansing = (
            path.read_text(encoding="utf-8") if exists else dump_spec(cleansing_draft(profile))
        )
        bus = DatasetIndex(repo, catalog).bus_matrix()
        return OnboardingDraft(
            source=source,
            patch=patch,
            proposer=proposer,
            note=note,
            cleansing=cleansing,
            cleansing_exists=exists,
            mermaid=mermaid(apply(ontology, patch)),
            bus=bus_with_candidates(bus, ontology, patch),
        )

    def check_request(self, source: str, patch: OntologyPatch, cleansing: str) -> list[str]:
        """確定の前に、追加案とクレンジングの定義を検証する（空なら確定してよい）。"""
        repo = self.config.repo_root
        catalog = Catalog(self.config.source_db, repo / "warehouse" / "target" / "manifest.json")
        ontology = load(repo / ONTOLOGY)
        errors = validate_patch(ontology, patch, catalog, source)
        try:
            spec = parse_cleansing(cleansing, source)
            errors += validate(spec, source_columns(catalog, source))
        except ValueError as e:
            errors.append(str(e))
        return errors

    # --- 確定（PR） --------------------------------------------------------------------------

    async def run(
        self,
        source: str,
        patch: OntologyPatch,
        cleansing: str,
        on_step: Callable[[StepUpdate], None] = lambda _: None,
    ) -> PipelineResult:
        labels = dict(ONBOARD_STEPS)
        current = ""

        def step(name: str, status: StepStatus, detail: str = "") -> None:
            nonlocal current
            current = name
            on_step(StepUpdate(name, labels[name], status, detail))

        result = PipelineResult(ok=False)
        worktree: Worktree | None = None
        pushed = False
        try:
            errors = self.check_request(source, patch, cleansing)
            if errors:
                raise PipelineError("確定できません: " + " / ".join(errors))
            spec = parse_cleansing(cleansing, source)
            self.github.check()

            step("worktree", StepStatus.RUNNING)
            wt = self.workspace.create_worktree(onboarding_branch(source), self.config.base)
            worktree = wt
            result.branch = wt.branch
            warehouse = prepare_warehouse(self.config, wt)
            step("worktree", StepStatus.DONE, wt.branch)

            step("write", StepStatus.RUNNING)
            ontology = apply(load(wt.path / ONTOLOGY), patch)
            (wt.path / ONTOLOGY).write_text(dump(ontology), encoding="utf-8")
            (wt.path / "docs" / "ontology.md").write_text(markdown(ontology), encoding="utf-8")
            path = spec_path(warehouse / "cleansing", source)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump_spec(spec), encoding="utf-8")
            catalog = Catalog(warehouse / "data" / "dev.duckdb")
            info = ProjectInfo(warehouse, catalog)
            # 定義（上で書いた）から stg を生成する。既に stg があれば上書きせずに止める
            info.write(staging_files(source, info))
            step(
                "write", StepStatus.DONE, f"概念 {len(patch.concepts)}、関係 {len(patch.relations)}"
            )

            step("verify", StepStatus.RUNNING)
            checked = await asyncio.to_thread(verify, warehouse, self.config.bin_dir, [])
            problems = [] if checked.ok else [checked.summary]
            onto_errors = check_ontology(ontology, catalog, warehouse / "target" / "manifest.json")
            # 取り込みで足した概念・関係に関わる食い違いだけを見る（既存の部分は CI の検査に任せる）
            new = [c.name for c in patch.concepts] + [r.verb for r in patch.relations]
            problems += [e for e in onto_errors.errors if any(n in e for n in new)]
            if problems:
                raise PipelineError(
                    " / ".join(problems) + (f"\n{checked.log}" if not checked.ok else "")
                )
            step("verify", StepStatus.DONE, checked.summary)

            step("commit", StepStatus.RUNNING)
            staged = self.workspace.stage(wt, ["warehouse", "ontology", "docs/ontology.md"])
            if not staged:
                raise PipelineError("commit するファイルがありません")
            result.files = staged
            self.workspace.commit(wt, f"feat(onboard): 源泉 {source} を取り込む")
            self.workspace.push(wt)
            pushed = True
            step("commit", StepStatus.DONE, f"{len(staged)} ファイル")

            step("pr", StepStatus.RUNNING)
            result.pr_url = self.github.create_pr(
                wt, f"feat(onboard): 源泉 {source} を取り込む", self._body(source, patch, staged)
            )
            step("pr", StepStatus.DONE, result.pr_url)
            result.ok = True
            return result
        except Exception as e:
            result.error = str(e)
            if current:
                step(current, StepStatus.FAILED, str(e)[:2000])
            if worktree is not None and not pushed:
                try:
                    self.workspace.remove_worktree(worktree, delete_branch=True)
                    result.branch = None
                except Exception:
                    pass
            return result

    @staticmethod
    def _body(source: str, patch: OntologyPatch, files: list[str]) -> str:
        concepts = "\n".join(
            f"- **{c.label}**（`{c.name}`、{'出来事' if c.kind == 'event' else 'もの・人'}）: "
            + "、".join(a.label for a in c.attributes)
            for c in patch.concepts
        )
        relations = "\n".join(
            f"- {r.from_} →{r.verb}→ {r.to}（`{r.via}`）" for r in patch.relations
        )
        listed = "\n".join(f"- `{f}`" for f in files)
        return (
            f"源泉 `{source}` を取り込みます（#69 の取り込みフロー）。\n\n"
            "## オントロジーへの追加\n"
            f"{concepts}\n\n### 関係\n{relations or '- なし'}\n\n"
            "## クレンジング層\n"
            f"`warehouse/cleansing/{source}.yml` の定義から stg を生成しました。\n\n"
            "## 検証\n- dbt build / dbt parse、オントロジーとクレンジングの検査\n\n"
            f"## 変更したファイル\n{listed}\n\n"
            "次は、この源泉の指標をインタビューで要求すると、fct / dim と指標ができます。\n"
        )
