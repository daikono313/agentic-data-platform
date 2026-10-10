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
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

COMMAND_TIMEOUT_S = 300

Runner = Callable[[Sequence[str], Path, dict[str, str] | None], subprocess.CompletedProcess[str]]


def run(
    args: Sequence[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(args),
            cwd=cwd,
            env=(os.environ | env) if env else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"{' '.join(args[:3])} が {COMMAND_TIMEOUT_S} 秒で終わりませんでした") from e
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
        url = self.git("remote", "get-url", "origin").rstrip("/")
        m = re.search(r"[:/]([^/:]+)/[^/]+?(?:\.git)?$", url)
        return m.group(1) if m else None

    def remove(self, wt: Worktree, path: str) -> None:
        """path を削除して stage する。"""
        self.git("rm", "-r", "-q", "--", path, cwd=wt.path)

    def _branch_exists(self, branch: str) -> bool:
        if self.git("branch", "--list", branch):
            return True
        return bool(self.git("ls-remote", "--heads", "origin", branch))

    def available_branch(self, branch: str) -> str:
        """ローカルにもリモートにも無いブランチ名にする（同じ日の再実行・同名イベントに備える）。"""
        candidate, n = branch, 1
        while (
            self._branch_exists(candidate)
            or (self.worktrees_dir / candidate.replace("/", "-")).exists()
        ):
            n += 1
            candidate = f"{branch}-{n}"
        return candidate

    def create_worktree(self, branch: str, base: str = "origin/main") -> Worktree:
        """base から branch（使用済みなら -2, -3 … を付ける）を作り、worktree に展開する。"""
        if base.startswith("origin/"):
            self.git("fetch", "origin", base.removeprefix("origin/"))
        branch = self.available_branch(branch)
        path = self.worktrees_dir / branch.replace("/", "-")
        self.git("worktree", "add", "-b", branch, str(path), base)
        return Worktree(path=path, branch=branch)

    def stage(self, wt: Worktree, pathspecs: Sequence[str]) -> list[str]:
        """pathspecs の下の変更（追加・変更・削除）をすべて stage し、そのパスを返す。"""
        existing = [p for p in pathspecs if (wt.path / p).exists()]
        if existing:
            self.git("add", "-A", "--", *existing, cwd=wt.path)
        out = self.run(["git", "diff", "--cached", "--name-only", "-z"], wt.path, None).stdout
        return sorted(p for p in out.split("\0") if p)

    def commit(self, wt: Worktree, message: str) -> str:
        """stage 済みの変更を commit する。author は git の設定（includeIf）に従う。"""
        self.git("commit", "-m", message, cwd=wt.path)
        return self.git("rev-parse", "HEAD", cwd=wt.path)

    def push(self, wt: Worktree) -> None:
        self.git("push", "-u", "origin", wt.branch, cwd=wt.path)

    def remove_worktree(self, wt: Worktree, delete_branch: bool = False) -> None:
        self.git("worktree", "remove", "--force", str(wt.path))
        if delete_branch:
            self.git("branch", "-D", wt.branch)


MAX_BODY = 60000
"""GitHub の PR 本文の上限（65536 文字）より少し小さく切る。"""


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
        if not token:
            raise GitError(f"{self.account} の gh トークンを取得できません")
        return {"GH_TOKEN": token}

    def check(self) -> None:
        """push する前に、PR を作れる状態か（アカウントとトークン）を確かめる。"""
        self._env()

    def repo_name(self, wt: Worktree) -> str | None:
        """GitHub 上の `<owner>/<repo>`（SSH のホストエイリアスの origin でも gh が解決する）。"""
        out = self.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            wt.path,
            self._env(),
        ).stdout.strip()
        return out if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", out) else None

    def create_pr(
        self, wt: Worktree, title: str, body: str, base: str = "main", draft: bool = False
    ) -> str:
        if len(body) > MAX_BODY:
            body = body[: MAX_BODY - 100] + "\n\n（本文が長すぎるため省略しました）\n"
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as f:
            f.write(body)
            body_file = f.name
        try:
            args = ["gh", "pr", "create", "--base", base, "--head", wt.branch, "--title", title]
            args += ["--body-file", body_file] + (["--draft"] if draft else [])
            result = self.run(args, wt.path, self._env())
        finally:
            os.unlink(body_file)
        return result.stdout.strip().splitlines()[-1]
