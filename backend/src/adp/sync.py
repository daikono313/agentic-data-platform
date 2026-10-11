"""マージ後の自動反映（#74）。

生成 PR・取り込み PR が GitHub でマージされても、手元（アプリが動いているチェックアウト）の
warehouse は古いまま。アプリから「main の新しい変更を取り込む → dbt build → 一覧を更新」を行う。

- 手元が main で、変更が無いときだけ `git pull --ff-only` する（作業中のブランチや変更は触らない）
- 反映は dbt deps / build / parse と、オントロジー・クレンジング・チャートの検査（`make dbt-build`
  と同じ）。一覧（/datasets）は manifest を読み直すので、終われば新しい指標が見える
- launchd / cron などシステムの仕組みは使わない（アプリの画面から実行する）
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from adp.generation.repair import _clean_log, _run
from adp.pipeline import PipelineResult, StepStatus, StepUpdate
from adp.vcs import GitWorkspace

SYNC_STEPS: list[tuple[str, str]] = [
    ("pull", "main の新しい変更を取り込む"),
    ("build", "dbt build（モデル・テスト）"),
    ("check", "検査（Semantic Layer・オントロジー・クレンジング・チャート）"),
]
FETCH_TTL_S = 60.0
"""状態を見るたびに git fetch しない（ネットワーク）。"""


class SyncStatus(BaseModel):
    branch: str
    clean: bool
    behind: int
    """origin/main にあって手元に無いコミットの数。"""
    can_pull: bool
    """手元が main で変更が無い（取り込める）。"""
    reason: str | None = None
    """取り込めない理由。"""


@dataclass
class _Cache:
    fetched_at: float = 0.0


@dataclass
class Sync:
    repo_root: Path
    bin_dir: Path
    workspace: GitWorkspace
    _cache: _Cache = field(default_factory=_Cache)

    def status(self, fetch: bool = True) -> SyncStatus:
        """手元と origin/main の差。fetch は FETCH_TTL_S に 1 回まで（失敗しても続ける）。"""
        git = self.workspace.git
        if fetch and time.monotonic() - self._cache.fetched_at > FETCH_TTL_S:
            try:
                git("fetch", "-q", "origin", "main")
                self._cache.fetched_at = time.monotonic()
            except Exception:
                pass
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        # 追跡していないファイル（.adp など ignore 済み以外）も含めて、変更が無いか
        clean = git("status", "--porcelain") == ""
        try:
            behind = int(git("rev-list", "--count", "HEAD..origin/main"))
        except Exception:
            behind = 0
        reason = None
        if branch != "main":
            reason = f"手元が main ではありません（{branch}）。main に切り替えると取り込めます"
        elif not clean:
            reason = "手元に変更があります。変更を片付けると取り込めます"
        return SyncStatus(
            branch=branch, clean=clean, behind=behind, can_pull=reason is None, reason=reason
        )

    async def run(self, on_step: Callable[[StepUpdate], None] = lambda _: None) -> PipelineResult:
        """取り込み（できるときだけ）→ dbt build → 検査。どこかで失敗したら止める。"""
        labels = dict(SYNC_STEPS)
        current = ""

        def step(name: str, status: StepStatus, detail: str = "") -> None:
            nonlocal current
            current = name
            on_step(StepUpdate(name, labels[name], status, detail))

        result = PipelineResult(ok=False)
        try:
            step("pull", StepStatus.RUNNING)
            status = await asyncio.to_thread(self.status)
            if status.behind and status.can_pull:
                await asyncio.to_thread(
                    self.workspace.git, "pull", "--ff-only", "-q", "origin", "main"
                )
                step("pull", StepStatus.DONE, f"{status.behind} 件のコミットを取り込みました")
            elif status.behind:
                raise SyncError(status.reason or "取り込めません")
            else:
                step("pull", StepStatus.DONE, "新しい変更はありません（dbt だけ作り直します）")

            warehouse = self.repo_root / "warehouse"
            dbt = str(self.bin_dir / "dbt")
            step("build", StepStatus.RUNNING)
            for args in ([dbt, "deps"], [dbt, "build"], [dbt, "parse"]):
                done = await asyncio.to_thread(_run, args, warehouse)
                if done.returncode != 0:
                    raise SyncError(
                        f"{' '.join(args[1:])} が失敗しました\n"
                        + _clean_log(done.stdout + done.stderr)[-2000:]
                    )
            step("build", StepStatus.DONE, "dbt build・parse が成功しました")

            step("check", StepStatus.RUNNING)
            python = str(self.bin_dir / "python")
            checks = [
                [python, "-m", f"adp.{m}", "check"] for m in ("charts", "ontology", "cleansing")
            ]
            if (warehouse / "target" / "semantic_manifest.json").exists():
                checks.insert(0, [str(self.bin_dir / "mf"), "validate-configs"])
            for args in checks:
                done = await asyncio.to_thread(_check, args, self.repo_root / "backend", warehouse)
                if done.returncode != 0:
                    raise SyncError(
                        f"{' '.join(args[-2:])} が失敗しました\n"
                        + _clean_log(done.stdout + done.stderr)[-2000:]
                    )
            step("check", StepStatus.DONE, "検査が成功しました。一覧は新しい内容になります")
            result.ok = True
            return result
        except Exception as e:
            result.error = str(e)
            if current:
                step(current, StepStatus.FAILED, str(e)[:2000])
            return result


class SyncError(Exception):
    pass


def _check(args: list[str], backend: Path, warehouse: Path) -> subprocess.CompletedProcess[str]:
    """検査のコマンド。mf は warehouse で、python -m adp.* は backend で動かす。"""
    if args[0].endswith("mf"):
        return _run(args, warehouse)
    env = os.environ | {"NO_COLOR": "1"}
    try:
        return subprocess.run(
            args, cwd=backend, env=env, capture_output=True, text=True, check=False, timeout=600
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "600 秒で打ち切りました")
