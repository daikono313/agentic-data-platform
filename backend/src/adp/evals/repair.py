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

結果は `.adp/evals/repair-<日時>-<pid>/<name>[.<n>].json` と、成功率の表（`summary.md`）。
"""

from __future__ import annotations

import importlib.util
import json
import os
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
from adp.generation.repair import Repairer, ensure_deps, test_inventory
from adp.llm import AgentRunner, LLMSettings
from adp.llm.settings import REPO_ROOT
from adp.warehouse import warehouse_db

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
        scenario = cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if scenario.name != path.stem:
            raise ValueError(f"{path.name}: name（{scenario.name}）はファイル名と同じにする")
        return scenario


@dataclass
class RepairEvalResult:
    scenario: str
    passed: bool
    ok: bool
    """自己修復で検証が通ったか。"""
    expect: str
    restored: bool = False
    """失敗を入れたファイルが、生成した直後の内容に戻ったか（別の直し方・回避をしていないか）。"""
    changed: list[str] = field(default_factory=list)
    """生成した直後から変わったファイル（warehouse からの相対パス）。"""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    final: str = ""
    duration_s: float = 0.0
    error: str | None = None


def strip_generated(warehouse: Path) -> None:
    """アプリが生成してマージされたモデル・テスト・チャートを除き、生成前の warehouse にする。

    源泉の宣言（`models/staging/_<source>__sources.yml`）は残す。クレンジング層（stg）と同じく
    生成した fct / dim より手前の基盤で、stg がそれを参照している（#104）。
    """
    generated = [warehouse / "models", warehouse / "tests", warehouse / "snapshots"]
    for path in [p for d in generated for p in d.rglob("*")]:
        if (
            path.is_file()
            and path.suffix in {".sql", ".yml"}
            and not path.name.endswith("__sources.yml")
            and path.read_text(encoding="utf-8").lstrip().startswith(GENERATED)
        ):
            path.unlink()
    for path in [*warehouse.glob("charts/*.yml"), *warehouse.glob("chart_specs/*.yml")]:
        path.unlink()


def prepare_warehouse(dest: Path, orders: int = 500) -> Path:
    """生成前の warehouse（アプリが生成したモデル・チャートを除く）と小さなサンプルデータ。"""
    warehouse = dest / "warehouse"
    shutil.copytree(
        REPO_ROOT / "warehouse",
        warehouse,
        ignore=shutil.ignore_patterns("target", "data", "logs", ".user.yml"),
    )
    strip_generated(warehouse)
    spec = importlib.util.spec_from_file_location(
        "adp_sample_data", warehouse / "scripts" / "generate_sample_data.py"
    )
    assert spec and spec.loader
    gen = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gen
    spec.loader.exec_module(gen)
    gen.load(warehouse_db(warehouse), gen.generate(orders, 3))
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
        catalog = Catalog(warehouse_db(warehouse))
        info = ProjectInfo(warehouse, catalog)
        beam = load(REPO_ROOT / scenario.beam)
        generation = generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam)
        info.write(generation.files)
        # 比べるテストの一覧は、失敗を入れる前の（生成した直後の）もの
        deps = ensure_deps(warehouse, bin_dir)
        if deps is not None:
            raise ValueError(f"dbt deps が失敗しました: {deps.log[-500:]}")
        baseline = test_inventory(warehouse, bin_dir)
        if baseline is None:
            raise ValueError("生成した直後のプロジェクトでテストの一覧を取れません")
        before = _snapshot(warehouse)
        scenario.fault.apply(warehouse)
        original = before[scenario.fault.file]
        repair = await Repairer(runner, settings, bin_dir).run(
            warehouse, generation.queries, baseline=baseline
        )
        result.ok = repair.ok
        result.final = repair.final.summary
        result.attempts = [
            {
                "model": a.model,
                "reason": a.reason,
                "ok": a.result.ok,
                "summary": a.result.summary,
                "agent_summary": a.agent_summary[-2000:],
                "denied": a.denied,
            }
            for a in repair.attempts
        ]
        after = _snapshot(warehouse)
        result.restored = after.get(scenario.fault.file) == original
        result.changed = sorted(
            p for p in before.keys() | after.keys() if before.get(p) != after.get(p)
        )
        if repair.ok and not repair.attempts:
            raise ValueError("失敗を入れても検証が通りました（シナリオの失敗が効いていない）")
        if scenario.expect == "fixed":
            result.passed = repair.ok
        else:  # 人に渡すべき失敗: 直ったことにせず、テストも弱めていない
            result.passed = not repair.ok and "弱められ" not in repair.final.summary
    except Exception as e:  # 準備の失敗はシナリオの失敗として記録する
        result.error = f"{type(e).__name__}: {e}"
    result.duration_s = round(time.monotonic() - started, 1)
    return result


def _snapshot(warehouse: Path) -> dict[str, str]:
    """models / tests / macros のファイルの内容（直し方の確認用）。"""
    out: dict[str, str] = {}
    for d in ("models", "tests", "macros"):
        for p in (warehouse / d).rglob("*"):
            if p.is_file() and p.suffix in {".sql", ".yml", ".yaml"}:
                out[str(p.relative_to(warehouse))] = p.read_text(encoding="utf-8")
    return out


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
    lines = [
        "| シナリオ | 期待どおり | 元に戻した | 修復の試行（平均） | 時間（平均） |",
        "|---|---|---|---|---|",
    ]
    for name, rs in sorted(by.items()):
        passed = sum(r.passed for r in rs)
        tries = sum(len(r.attempts) for r in rs) / len(rs)
        secs = sum(r.duration_s for r in rs) / len(rs)
        restored = sum(r.restored for r in rs)
        lines.append(
            f"| {name} | {passed}/{len(rs)} | {restored}/{len(rs)} | {tries:.1f} | {secs:.0f} 秒 |"
        )
    total = sum(r.passed for r in results)
    lines.append(f"| **合計** | **{total}/{len(results)}** | | | |")
    return "\n".join(lines)


def default_out_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return REPO_ROOT / ".adp" / "evals" / f"repair-{stamp}-{os.getpid()}"
