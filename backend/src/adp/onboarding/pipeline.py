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
from dataclasses import replace
from datetime import date
from typing import Any

import yaml

from adp.catalog.catalog import Catalog
from adp.cleansing.check import column_kinds, date_derives, source_columns, validate
from adp.cleansing.check import draft as cleansing_draft
from adp.cleansing.models import CleansingSpec, dump_spec, spec_path
from adp.cleansing.render import SourceColumn
from adp.datasets import DatasetIndex
from adp.generation.dbt import staging_files
from adp.generation.project import ProjectInfo
from adp.generation.repair import verify
from adp.llm import AgentRunner, LLMSettings
from adp.markdown import safe
from adp.onboarding.agent import propose_with_agent
from adp.onboarding.plan import OnboardingDraft, bus_with_candidates
from adp.onboarding.proposal import (
    OntologyPatch,
    apply,
    patch_errors,
    propose_from_profile,
    validate_patch,
)
from adp.ontology.check import check as check_ontology
from adp.ontology.models import Ontology, dump, load
from adp.ontology.render import markdown, mermaid
from adp.pipeline import (
    ONTOLOGY,
    JobSteps,
    PipelineConfig,
    PipelineError,
    PipelineResult,
    StepStatus,
    StepUpdate,
    prepare_warehouse,
)
from adp.vcs import GitHub, GitWorkspace
from adp.warehouse import warehouse_db


class AlreadyOnboarded(Exception):
    """既にオントロジーに概念がある源泉を取り込もうとした。"""


ONBOARD_STEPS: list[tuple[str, str]] = [
    ("worktree", "作業ブランチを作成"),
    ("write", "オントロジーとクレンジングの定義を書く"),
    ("verify", "クレンジング層を検証（dbt build）"),
    ("commit", "commit / push"),
    ("pr", "PR を作成"),
]


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


def _spec_or_none(text: str, source: str) -> CleansingSpec | None:
    """画面のフォームで直せる定義。読めなければ None（画面では YAML で直す）。"""
    try:
        return parse_cleansing(text, source)
    except ValueError:
        return None


CLEANSING_MAX = 20000
"""画面から受け取るクレンジングの定義（YAML）の長さの上限。"""


def cleansing_text(cleansing: str | dict[str, Any]) -> str:
    """画面から受け取ったクレンジングの定義（YAML か、フォームで直した JSON）を YAML にそろえる。

    JSON は CleansingSpec で検証してから書き出す（形式の誤りは ValueError）。
    """
    if isinstance(cleansing, str):
        return cleansing
    text = dump_spec(CleansingSpec.model_validate(cleansing))
    if len(text) > CLEANSING_MAX:
        raise ValueError(f"クレンジングの定義が長すぎます（{CLEANSING_MAX} 文字まで）")
    return text


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
        """追加案（エージェント → 失敗したらプロファイルから）・クレンジングの定義・バスの差分。

        既にオントロジーにある源泉は AlreadyOnboarded、オントロジーが無ければ FileNotFoundError。
        """
        repo = self.config.repo_root
        catalog = Catalog(self.config.source_db, repo / "warehouse" / "target" / "manifest.json")
        ontology = await asyncio.to_thread(load, repo / ONTOLOGY)
        profile = await asyncio.to_thread(catalog.profile_table, source)
        source = f"{profile.schema_name}.{profile.name}"  # raw を省いた指定も正規化してから調べる
        if any(c.source == source for c in ontology.concepts):
            raise AlreadyOnboarded(f"{source} は既にオントロジーにあります")
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
        bus = await asyncio.to_thread(DatasetIndex(repo, catalog).bus_matrix)
        merged = apply(ontology, patch)
        columns = [SourceColumn(c.name, c.data_type) for c in profile.columns]
        spec = _spec_or_none(cleansing, source)
        return OnboardingDraft(
            source=source,
            patch=patch,
            proposer=proposer,
            note=note,
            cleansing=cleansing,
            cleansing_exists=exists,
            cleansing_spec=(
                None
                if spec is None
                else spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
            ),
            column_kinds=column_kinds(spec, columns),
            date_derives={} if spec is None else date_derives(spec, columns),
            columns=profile.profiles,
            concept_labels={c.name: c.label for c in merged.concepts},
            mermaid=mermaid(merged),
            bus=bus_with_candidates(bus, ontology, patch),
        )

    def check_request(
        self, source: str, patch: OntologyPatch, cleansing: str, ontology: Ontology | None = None
    ) -> list[str]:
        """確定の前に、追加案とクレンジングの定義を検証する（空なら確定してよい）。

        ontology を渡せばそれ（取り込みの作業ブランチのもの）で、無ければ手元の正本で調べる。
        """
        repo = self.config.repo_root
        catalog = Catalog(self.config.source_db, repo / "warehouse" / "target" / "manifest.json")
        errors = validate_patch(ontology or load(repo / ONTOLOGY), patch, catalog, source)
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
        job = JobSteps(ONBOARD_STEPS, on_step, self.workspace, detail_limit=2000)
        step = job.step
        result = PipelineResult(ok=False)
        # git・DB のコピー・dbt などの重い処理はスレッドで行う（進捗の SSE などを止めない）
        thread = asyncio.to_thread
        try:
            errors = await thread(self.check_request, source, patch, cleansing)
            if errors:
                raise PipelineError("確定できません: " + " / ".join(errors))
            spec = parse_cleansing(cleansing, source)
            await thread(self.github.check)

            step("worktree", StepStatus.RUNNING)
            wt = await job.create_worktree(onboarding_branch(source), self.config.base)
            result.branch = wt.branch
            # 手元ではなく、取り込む先（main）のオントロジーで確かめ直す（既に取り込み済みなど）
            base_ontology = load(wt.path / ONTOLOGY)
            errors = await thread(self.check_request, source, patch, cleansing, base_ontology)
            if errors:
                raise PipelineError("確定できません: " + " / ".join(errors))
            warehouse = await thread(prepare_warehouse, self.config, wt)
            step("worktree", StepStatus.DONE, wt.branch)

            step("write", StepStatus.RUNNING)
            ontology = apply(base_ontology, patch)
            (wt.path / ONTOLOGY).write_text(dump(ontology), encoding="utf-8")
            (wt.path / "docs" / "ontology.md").write_text(markdown(ontology), encoding="utf-8")
            path = spec_path(warehouse / "cleansing", source)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump_spec(spec), encoding="utf-8")
            catalog = Catalog(warehouse_db(warehouse))
            info = ProjectInfo(warehouse, catalog)
            # 定義（上で書いた）から stg を生成する。既にある stg（make cleansing で作ったもの
            # など）は定義から作り直す（stg は定義の生成物。ADR 0004）
            files = [
                replace(f, replaces=(warehouse / f.path).read_text(encoding="utf-8"))
                if f.path.startswith("models/staging/stg_") and (warehouse / f.path).exists()
                else f
                for f in staging_files(source, info)
            ]
            info.write(files)
            step(
                "write", StepStatus.DONE, f"概念 {len(patch.concepts)}、関係 {len(patch.relations)}"
            )

            step("verify", StepStatus.RUNNING)
            checked = await thread(verify, warehouse, self.config.bin_dir, [])
            problems = [] if checked.ok else [checked.summary]
            onto_errors = await thread(
                check_ontology, ontology, catalog, warehouse / "target" / "manifest.json"
            )
            # 取り込みで足した概念・関係に関わる食い違いだけを見る（既存の部分は CI の検査に任せる）
            problems += patch_errors(onto_errors.errors, patch)
            if problems:
                raise PipelineError(
                    " / ".join(problems) + (f"\n{checked.log}" if not checked.ok else "")
                )
            step("verify", StepStatus.DONE, checked.summary)

            step("commit", StepStatus.RUNNING)
            staged = await job.commit_and_push(
                wt,
                ["warehouse", "ontology", "docs/ontology.md"],
                f"feat(onboard): 源泉 {source} を取り込む",
            )
            result.files = staged
            step("commit", StepStatus.DONE, f"{len(staged)} ファイル")

            step("pr", StepStatus.RUNNING)
            result.pr_url = await thread(
                self.github.create_pr,
                wt,
                f"feat(onboard): 源泉 {source} を取り込む",
                self._body(source, patch, staged),
            )
            step("pr", StepStatus.DONE, result.pr_url)
            result.ok = True
            return result
        except Exception as e:
            return await job.fail(result, e)

    @staticmethod
    def _body(source: str, patch: OntologyPatch, files: list[str]) -> str:
        # 表示名・動詞は LLM やユーザーの文章。#N・@name・コードブロックを無効にする（guardrails）
        kinds = {"event": "出来事", "entity": "もの・人"}
        concepts = "\n".join(
            f"- **{safe(c.label)}**（`{c.name}`、{kinds[c.kind]}）: "
            + "、".join(safe(a.label) for a in c.attributes)
            for c in patch.concepts
        )
        relations = "\n".join(
            f"- {r.from_} →{safe(r.verb)}→ {r.to}（`{r.via}`）" for r in patch.relations
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
