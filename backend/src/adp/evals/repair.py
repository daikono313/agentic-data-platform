"""生成の自己修復の eval（#78）。

実際に起きた失敗の型（SQL の誤り・指標が使う列の欠け・参照の誤りなど）を、
生成した dbt プロジェクトにわざと入れ、自己修復（`adp.generation.repair.Repairer`）が
直せるかを測る。シナリオは
`evals/repair/<name>.yml`:

    name: sql_typo
    beam: backend/tests/fixtures/beam/sales_by_store.yml   # リポジトリからの相対パス
    fault:
      file: models/marts/fct_order_lines.sql               # warehouse からの相対パス
      replace: ["select", "selec"]                          # 最初の 1 か所を置き換える
      # または delete_lines_with: " as sales_amount"        # その文字列を含む行を消す
    expect: fixed                          # fixed（直る）か handed_to_human（人に渡す）

結果は `.adp/evals/repair-<日時>/<name>[-<n>].json` と、成功率の表。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.project import ProjectInfo
from adp.generation.repair import Repairer
from adp.llm import AgentRunner, LLMSettings
from adp.llm.settings import REPO_ROOT

SCENARIO_DIR = REPO_ROOT / "evals" / "repair"
GENERATED = ("-- adp により生成", "# adp により生成")


class Fault(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(pattern=r"^[A-Za-z0-9_./-]+$")
    replace: tuple[str, str] | None = None
    delete_lines_with: str | None = None

    def apply(self, warehouse: Path) -> None:
        path = (warehouse / self.file).resolve()
        if not path.is_relative_to(warehouse.resolve()):
            raise ValueError(f"warehouse の外のファイル: {self.file}")
        text = path.read_text(encoding="utf-8")
        if self.replace is not None:
            old, new = self.replace
            if old not in text:
                raise ValueError(f"{self.file} に {old!r} がありません（シナリオが古い）")
            text = text.replace(old, new, 1)
        elif self.delete_lines_with is not None:
            lines = text.splitlines(keepends=True)
            kept = [line for line in lines if self.delete_lines_with not in line]
            if len(kept) == len(lines):
                raise ValueError(f"{self.file} に {self.delete_lines_with!r} の行がありません")
            text = "".join(kept)
        else:
            raise ValueError("fault には replace か delete_lines_with を書く")
        path.write_text(text, encoding="utf-8")


class RepairScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    beam: str
    fault: Fault
    expect: Literal["fixed", "handed_to_human"] = "fixed"

    @classmethod
    def load(cls, path: Path) -> RepairScenario:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass
class RepairEvalResult:
    scenario: str
    passed: bool
    ok: bool
    """自己修復で検証が通ったか。"""
    expect: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    final: str = ""
    duration_s: float = 0.0
    error: str | None = None


def prepare_warehouse(dest: Path, orders: int = 500) -> Path:
    """生成前の warehouse（アプリが生成したモデル・チャートを除く）と小さなサンプルデータ。"""
    warehouse = dest / "warehouse"
    shutil.copytree(
        REPO_ROOT / "warehouse",
        warehouse,
        ignore=shutil.ignore_patterns("target", "data", "logs", ".user.yml"),
    )
    for path in [*(warehouse / "models").rglob("*"), *(warehouse / "tests").rglob("*")]:
        if (
            path.is_file()
            and path.suffix in {".sql", ".yml"}
            and path.read_text(encoding="utf-8").lstrip().startswith(GENERATED)
        ):
            path.unlink()
    for path in [*warehouse.glob("charts/*.yml"), *warehouse.glob("chart_specs/*.yml")]:
        path.unlink()
    spec = importlib.util.spec_from_file_location(
        "adp_sample_data", warehouse / "scripts" / "generate_sample_data.py"
    )
    assert spec and spec.loader
    gen = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gen
    spec.loader.exec_module(gen)
    gen.load(warehouse / "data" / "dev.duckdb", gen.generate(orders, 3))
    return warehouse


async def run_scenario(
    scenario: RepairScenario,
    runner: AgentRunner,
    settings: LLMSettings,
    bin_dir: Path,
    work: Path,
) -> RepairEvalResult:
    """生成 → 失敗を入れる → 自己修復。結果を期待（直る / 人に渡す）と比べる。"""
    started = time.monotonic()
    result = RepairEvalResult(scenario.name, passed=False, ok=False, expect=scenario.expect)
    try:
        warehouse = prepare_warehouse(work)
        catalog = Catalog(warehouse / "data" / "dev.duckdb")
        info = ProjectInfo(warehouse, catalog)
        beam = load(REPO_ROOT / scenario.beam)
        generation = generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam)
        info.write(generation.files)
        scenario.fault.apply(warehouse)
        repair = await Repairer(runner, settings, bin_dir).run(warehouse, generation.queries)
        result.ok = repair.ok
        result.final = repair.final.summary
        result.attempts = [
            {"model": a.model, "reason": a.reason, "ok": a.result.ok, "summary": a.result.summary}
            for a in repair.attempts
        ]
        result.passed = repair.ok == (scenario.expect == "fixed")
    except Exception as e:  # 準備の失敗はシナリオの失敗として記録する
        result.error = f"{type(e).__name__}: {e}"
    result.duration_s = round(time.monotonic() - started, 1)
    return result


def save(result: RepairEvalResult, out_dir: Path, suffix: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.scenario}{suffix}.json"
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def summary(results: list[RepairEvalResult]) -> str:
    """シナリオごとの成功率と、直るまでの試行回数の平均（Markdown の表）。"""
    by: dict[str, list[RepairEvalResult]] = {}
    for r in results:
        by.setdefault(r.scenario, []).append(r)
    lines = ["| シナリオ | 期待どおり | 修復の試行（平均） | 時間（平均） |", "|---|---|---|---|"]
    for name, rs in sorted(by.items()):
        passed = sum(r.passed for r in rs)
        tries = sum(len(r.attempts) for r in rs) / len(rs)
        secs = sum(r.duration_s for r in rs) / len(rs)
        lines.append(f"| {name} | {passed}/{len(rs)} | {tries:.1f} | {secs:.0f} 秒 |")
    total = sum(r.passed for r in results)
    lines.append(f"| **合計** | **{total}/{len(results)}** | | |")
    return "\n".join(lines)


def default_out_dir() -> Path:
    return REPO_ROOT / ".adp" / "evals" / f"repair-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
