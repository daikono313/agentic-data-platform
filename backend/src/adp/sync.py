"""マージ後の反映（#74）と自動反映（#106）。

生成 PR・取り込み PR が GitHub でマージされても、手元（アプリが動いているチェックアウト）の
warehouse は古いまま。「main の新しい変更を取り込む → dbt build → 一覧を更新」を行う。

- 手元が main で、変更が無いときだけ、確かめた origin/main のコミットへ `git merge --ff-only`
  する（作業中のブランチや変更は触らない）
- 反映は dbt deps / build / parse と、オントロジー・クレンジング・チャートの検査（`make dbt-build`
  と同じ）。一覧（/datasets）は manifest を読み直すので、終われば新しい指標が見える
- 自動反映（AutoSync）: backend が `ADP_AUTO_SYNC_INTERVAL_S` 秒（既定 60）ごとにマージを確かめ、
  取り込めるなら人のクリックなしに取り込みと dbt build を始める。インタビューの途中（応答中・
  直近 5 分に操作あり）は始めない。`ADP_AUTO_SYNC_INTERVAL_S=0` で止め、画面の「取り込んで
  更新する」だけにする
- launchd / cron などシステムの仕組みは使わない（アプリのプロセスの中で動かす）
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from adp.generation.repair import _clean_log, _run
from adp.pipeline import PipelineResult, StepStatus, StepUpdate
from adp.vcs import GitWorkspace
from adp.vcs.git import GIT_LOCK

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
    target: str | None = None
    """差を確かめた origin/main のコミット。取り込むのはこのコミットまで（確かめた後の fetch で
    origin/main が進んでも、確かめていないコミットは取り込まない）。"""


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
    _fetch_lock: threading.Lock = field(default_factory=threading.Lock)
    """GET /sync・自動反映の確認・反映ジョブが同時に fetch しないように。"""

    def _fetch_if_stale(self) -> None:
        with self._fetch_lock:  # 待っていた側は、先の fetch の結果を使う
            if time.monotonic() - self._cache.fetched_at > FETCH_TTL_S:
                self._fetch()

    def _fetch(self) -> None:
        """origin/main を取る。対話（パスフレーズ・ホストの確認）をしない・短く打ち切る。"""
        env = os.environ | {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
        }
        try:
            # ジョブの worktree の追加・push などと、refs や .git の lock ファイルを
            # 取り合わない（#110）。lock の順序は常に _fetch_lock → GIT_LOCK
            # （GIT_LOCK を持ったまま _fetch_lock を取る箇所は無い）
            with GIT_LOCK:
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
        if fetch:
            self._fetch_if_stale()
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        clean = git("status", "--porcelain") == ""
        target: str | None
        try:
            # 先にコミットを決め、差・変わるファイル・取り込みを、すべてこのコミットで見る
            target = git("rev-parse", "--verify", "-q", "origin/main^{commit}") or None
        except Exception:
            target = None
        try:
            behind = int(git("rev-list", "--count", f"HEAD..{target}")) if target else 0
            changed = git("diff", "--name-only", f"HEAD...{target}").splitlines() if behind else []
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
            target=target,
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
            if status.behind and status.target:
                # 確かめたコミットへ早送りするだけ（マージは作らない）。origin/main を名前で
                # 指すと、確かめた後の fetch で進んだ（アプリのコードを含むかもしれない）
                # コミットまで取り込んでしまう
                await asyncio.to_thread(
                    self.workspace.git, "merge", "--ff-only", "-q", status.target
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


AUTO_INTERVAL_S = 60.0
"""マージを確かめる間隔（#106）。.env の ADP_AUTO_SYNC_INTERVAL_S で変える。0 で自動にしない。"""
INTERVIEW_IDLE_S = 300.0
"""インタビューの操作からこの秒数が経つまでは自動で反映しない（dbt build の間はカタログを
読めず、インタビューの tool が失敗するため）。"""


class AutoSyncState(BaseModel):
    enabled: bool
    interval_s: float
    last_checked_at: str | None = None
    last_note: str | None = None
    """直近の確認で取り込まなかった理由（取り込めない・ほかのジョブを実行中など）。"""


@dataclass
class AutoSync:
    """マージされた PR を一定の間隔で確かめ、取り込めるなら反映ジョブを始める（#106）。

    取り込めない（手元が main 以外・変更あり・アプリのコードを含む）ときは何もせず、理由を残す
    （画面に出す）。ジョブを始めるのは start（SessionService.start_sync。ほかのジョブと重ねない）。
    """

    sync: Sync
    start: Callable[[], object]
    interval_s: float = AUTO_INTERVAL_S
    blocked: Callable[[], str | None] = lambda: None
    """自動で始めない理由（インタビューの途中など）。None なら始めてよい。"""
    state: AutoSyncState = field(init=False)

    def __post_init__(self) -> None:
        self.state = AutoSyncState(enabled=self.interval_s > 0, interval_s=self.interval_s)

    def tick(self, status: SyncStatus | None = None) -> bool:
        """1 回確かめる。反映ジョブを始めたら True。

        start はイベントループの上で呼ぶ（ジョブを asyncio のタスクとして始めるため）。状態の確認
        （git fetch）だけを別スレッドで行い、その結果を渡す。
        """
        from datetime import UTC, datetime

        self.state.last_checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        status = status if status is not None else self.sync.status()
        fetch_note = (
            f"origin の最新を取れませんでした: {status.fetch_error}" if status.fetch_error else None
        )

        def note(text: str | None) -> None:
            self.state.last_note = "。".join(t for t in (text, fetch_note) if t) or None

        if status.behind == 0:
            note(None)
            return False
        if not status.can_pull:
            note(status.reason)
            return False
        reason = self.blocked()
        if reason:
            note(f"{reason}。終わってから自動で取り込みます")
            return False
        try:
            self.start()
        except Exception as e:  # ほかのジョブを実行中など。次の確認でもう一度
            note(str(e))
            return False
        note(None)
        return True

    async def loop(self) -> None:
        while self.state.enabled:
            await asyncio.sleep(self.interval_s)
            try:
                self.tick(await asyncio.to_thread(self.sync.status))
            except Exception as e:  # git が使えないなど。止めずに次へ
                self.state.last_note = f"確かめられませんでした: {type(e).__name__}"


def auto_interval(values: dict[str, str]) -> float:
    """`ADP_AUTO_SYNC_INTERVAL_S`（.env か環境変数）。数でなければ既定値。"""
    raw = values.get("ADP_AUTO_SYNC_INTERVAL_S") or os.environ.get("ADP_AUTO_SYNC_INTERVAL_S")
    try:
        return max(0.0, float(raw)) if raw else AUTO_INTERVAL_S
    except ValueError:
        print(
            f"ADP_AUTO_SYNC_INTERVAL_S は秒数（0 で無効）で指定してください: {raw!r}。"
            f"{AUTO_INTERVAL_S:g} 秒にします",
            file=sys.stderr,
        )
        return AUTO_INTERVAL_S


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
