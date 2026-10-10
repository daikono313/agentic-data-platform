"""生成用の git worktree、commit、push、PR 作成。

- 作業はリポジトリ内の `.adp/worktrees/<branch>` に限る（開発中のチェックアウトに触れない）。
- push と PR 作成のアカウントは、origin の所有者（例: daikono313）の gh トークンを使う。
  gh のアクティブなアカウントには依存しない。
- 既存ブランチへの force push はしない。
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

Runner = Callable[[Sequence[str], Path, dict[str, str] | None], subprocess.CompletedProcess[str]]


def run(
    args: Sequence[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        env=(os.environ | env) if env else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"{' '.join(args[:3])} が失敗しました: {result.stderr.strip()[-500:]}")
    return result


class GitError(Exception):
    pass


def branch_name(beam_name: str, event_name: str | None = None, today: date | None = None) -> str:
    """design-doc R15: beam/<event>-<yyyymmdd>"""
    stem = re.sub(r"[^a-z0-9_-]", "-", (event_name or beam_name).lower())
    return f"beam/{stem}-{(today or date.today()).strftime('%Y%m%d')}"


@dataclass
class Worktree:
    path: Path
    branch: str


class GitWorkspace:
    def __init__(self, repo_root: Path, runner: Runner = run) -> None:
        self.repo_root = repo_root
        self.run = runner

    @property
    def worktrees_dir(self) -> Path:
        return self.repo_root / ".adp" / "worktrees"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return self.run(["git", *args], cwd or self.repo_root, None).stdout.strip()

    def remote_owner(self) -> str | None:
        url = self.git("remote", "get-url", "origin")
        m = re.search(r"[:/]([^/:]+)/[^/]+?(?:\.git)?$", url)
        return m.group(1) if m else None

    def create_worktree(self, branch: str, base: str = "origin/main") -> Worktree:
        """base から branch を作り、`.adp/worktrees/<branch>` に展開する。"""
        if self.git("branch", "--list", branch):
            raise GitError(f"ブランチ {branch} は既にあります")
        path = self.worktrees_dir / branch.replace("/", "-")
        if path.exists():
            raise GitError(f"{path} は既にあります")
        if base.startswith("origin/"):
            self.git("fetch", "origin", base.removeprefix("origin/"))
        self.git("worktree", "add", "-b", branch, str(path), base)
        return Worktree(path=path, branch=branch)

    def commit(self, wt: Worktree, paths: Sequence[str], message: str) -> str:
        """指定したパスだけを commit する。author は git の設定（includeIf）に従う。"""
        if not paths:
            raise GitError("commit するファイルがありません")
        self.git("add", "--", *paths, cwd=wt.path)
        self.git("commit", "-m", message, cwd=wt.path)
        return self.git("rev-parse", "HEAD", cwd=wt.path)

    def push(self, wt: Worktree) -> None:
        self.git("push", "-u", "origin", wt.branch, cwd=wt.path)

    def remove_worktree(self, wt: Worktree) -> None:
        self.git("worktree", "remove", "--force", str(wt.path))


class GitHub:
    """gh CLI で PR を作る。トークンは origin の所有者のもの。"""

    def __init__(self, repo_root: Path, account: str | None, runner: Runner = run) -> None:
        self.repo_root = repo_root
        self.account = account
        self.run = runner

    def _env(self) -> dict[str, str]:
        if not self.account:
            raise GitError("PR を作る GitHub アカウントが決まっていません")
        token = self.run(
            ["gh", "auth", "token", "-u", self.account], self.repo_root, None
        ).stdout.strip()
        return {"GH_TOKEN": token}

    def create_pr(
        self, wt: Worktree, title: str, body: str, base: str = "main", draft: bool = False
    ) -> str:
        args = ["gh", "pr", "create", "--base", base, "--head", wt.branch, "--title", title]
        args += ["--body", body] + (["--draft"] if draft else [])
        result = self.run(
            args,
            wt.path,
            self._env(),
        )
        return result.stdout.strip().splitlines()[-1]
