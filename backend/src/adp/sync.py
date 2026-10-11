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
import re
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
"""状態を見るたびに git fetch しない（ネットワーク）。失敗しても次の試行まで待つ。"""
FETCH_TIMEOUT_S = 20
APP_PATHS = ("backend/", "frontend/", "Makefile")
"""ここが変わる取り込みは画面からはしない（サーバーの再読み込みでジョブが止まり、依存の入れ直しも
要るため。git pull・make setup・再起動を人が行う）。"""


class SyncStatus(BaseModel):
    branch: str
    clean: bool
    behind: int
    """origin/main にあって手元に無いコミットの数。"""
    can_pull: bool
    """手元が main で変更が無く、アプリのコードの変更を含まない（画面から取り込める）。"""
    can_rebuild: bool
    """手元が main で変更が無い（dbt の作り直しだけはできる）。"""
    reason: str | None = None
    """取り込めない理由。"""
    fetch_error: str | None = None
    """origin の最新を取れなかった理由（差は前回の情報）。"""


@dataclass
class _Cache:
    fetched_at: float = 0.0
    error: str | None = None


@dataclass
class Sync:
    repo_root: Path
    bin_dir: Path
    workspace: GitWorkspace
    _cache: _Cache = field(default_factory=_Cache)

    def _fetch(self) -> None:
        """origin/main を取る。対話（パスフレーズ・ホストの確認）をしない・短く打ち切る。"""
        env = os.environ | {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
        }
        try:
            done = subprocess.run(
                ["git", "fetch", "-q", "origin", "main"],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=FETCH_TIMEOUT_S,
            )
            self._cache.error = (
                None if done.returncode == 0 else (done.stderr.strip()[-300:] or "失敗")
            )
        except subprocess.TimeoutExpired:
            self._cache.error = f"{FETCH_TIMEOUT_S} 秒で打ち切りました"
        # 失敗しても、すぐには繰り返さない（画面を開くたびに待たせない）
        self._cache.fetched_at = time.monotonic()

    def status(self, fetch: bool = True) -> SyncStatus:
        """手元と origin/main の差。fetch は FETCH_TTL_S に 1 回まで。"""
        git = self.workspace.git
        if fetch and time.monotonic() - self._cache.fetched_at > FETCH_TTL_S:
            self._fetch()
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        clean = git("status", "--porcelain") == ""
        try:
            behind = int(git("rev-list", "--count", "HEAD..origin/main"))
            changed = (
                git("diff", "--name-only", "HEAD...origin/main").splitlines() if behind else []
            )
        except Exception:
            behind, changed = 0, []
        reason = None
        if branch != "main":
            reason = f"手元が main ではありません（{branch}）。main に切り替えると取り込めます"
        elif not clean:
            reason = "手元に変更があります。変更を片付けると取り込めます"
        app = [p for p in changed if p.startswith(APP_PATHS) or p == "Makefile"]
        if reason is None and app:
            reason = (
                "アプリのコードが更新されています（"
                + ", ".join(app[:3])
                + (" など" if len(app) > 3 else "")
                + "）。git pull・make setup の後、アプリを起動し直してください"
            )
        return SyncStatus(
            branch=branch,
            clean=clean,
            behind=behind,
            can_pull=reason is None,
            can_rebuild=branch == "main" and clean,
            reason=reason,
            fetch_error=self._cache.error,
        )

    async def run(self, on_step: Callable[[StepUpdate], None] = lambda _: None) -> PipelineResult:
        """取り込み（できるときだけ）→ dbt build → 検査。どこかで失敗したら止める。

        手元が main で変更が無いときだけ動く（dbt の作り直しも、共有の DB を作業中の内容で
        作り直さないため）。
        """
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
            if not status.can_rebuild:
                raise SyncError(status.reason or "手元が main で変更が無いときだけ反映できます")
            if status.behind and not status.can_pull:
                raise SyncError(status.reason or "取り込めません")
            if status.behind:
                # fetch 済みの origin/main へ早送りするだけ（マージは作らない）
                await asyncio.to_thread(
                    self.workspace.git, "merge", "--ff-only", "-q", "origin/main"
                )
                step("pull", StepStatus.DONE, f"{status.behind} 件のコミットを取り込みました")
            else:
                note = "新しい変更はありません（dbt だけ作り直します）"
                if status.fetch_error:
                    note += f"。origin の最新を取れませんでした: {status.fetch_error}"
                step("pull", StepStatus.DONE, note)

            warehouse = self.repo_root / "warehouse"
            (warehouse / "data").mkdir(parents=True, exist_ok=True)
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
            if _has_metrics(warehouse):  # make dbt-build と同じく、指標があるときだけ
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


def _has_metrics(warehouse: Path) -> bool:
    return any(
        re.search(r"^ *metrics:", p.read_text(encoding="utf-8"), re.M)
        for p in (warehouse / "models").rglob("*.yml")
    )


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
