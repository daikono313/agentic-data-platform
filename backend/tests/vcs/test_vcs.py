import subprocess
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from adp.beam import load
from adp.design import DesignBuilder
from adp.generation import AgentTask
from adp.vcs import GitHub, GitWorkspace, Verification, branch_name, pr_body, pr_title
from adp.vcs.git import GitError
from tests.design.test_builder import FakeSchema

FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "sales_by_store.yml"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote" / "daikono313" / "repo.git"
    remote.mkdir(parents=True)
    git(remote, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    git(work, "config", "commit.gpgsign", "false")
    (work / "README.md").write_text("x")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    return work


def test_branch_name() -> None:
    assert branch_name("sales", "customer_orders_product", date(2026, 10, 10)) == (
        "beam/customer_orders_product-20261010"
    )


def test_worktree_commit_push(repo: Path) -> None:
    ws = GitWorkspace(repo)
    assert ws.remote_owner() == "daikono313"
    wt = ws.create_worktree("beam/x-20261010")
    assert wt.path == repo / ".adp" / "worktrees" / "beam-x-20261010"
    (wt.path / "warehouse").mkdir()
    (wt.path / "warehouse" / "a.sql").write_text("select 1")
    (wt.path / "other.txt").write_text("not committed")
    sha = ws.commit(wt, ["warehouse/a.sql"], "feat: a")
    ws.push(wt)
    assert git(repo, "ls-remote", "origin", "beam/x-20261010").startswith(sha)
    assert "other.txt" in git(wt.path, "status", "--porcelain")
    with pytest.raises(GitError, match="既にあります"):
        ws.create_worktree("beam/x-20261010")
    ws.remove_worktree(wt)
    assert not wt.path.exists()


def test_pr_uses_owner_token_not_active_account(repo: Path) -> None:
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake(
        args: Sequence[str], cwd: Path, env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), env))
        out = (
            "tok123"
            if list(args)[:3] == ["gh", "auth", "token"]
            else "https://github.com/o/r/pull/9\n"
        )
        return subprocess.CompletedProcess(args, 0, out, "")

    from adp.vcs import Worktree

    gh = GitHub(repo, "daikono313", fake)
    url = gh.create_pr(Worktree(repo, "beam/x"), "t", "b")
    assert url == "https://github.com/o/r/pull/9"
    assert calls[0][0] == ["gh", "auth", "token", "-u", "daikono313"]
    assert calls[1][1] == {"GH_TOKEN": "tok123"}
    with pytest.raises(GitError):
        GitHub(repo, None, fake).create_pr(Worktree(repo, "b"), "t", "b")


def test_pr_body_contains_review_material() -> None:
    beam = load(FIXTURE)
    design = DesignBuilder(FakeSchema()).build(beam)
    body = pr_body(
        beam,
        design,
        ["warehouse/models/marts/fct_order_lines.sql"],
        Verification(True, "PASS=87", [("revenue by metric_time__day,store__store_name", True)]),
        [AgentTask("add_dim_columns", "dim_store", "既存の dim_store に列 opened_on を追加する")],
    )
    assert pr_title(beam) == "feat(beam): 店舗別の売上"
    for expected in (
        "店舗ごとの売上を見たい",
        "## BEAM Canvas",
        "Who（誰が）",
        "年収（データなし）",
        "## Event Matrix",
        "```mermaid",
        "erDiagram",
        "fct_order_lines.sql",
        "✅ 成功",
        "data_unavailable: 顧客の年収データがソースに無い",
        "要対応（add_dim_columns）",
        "beam/sales_by_store.yml",
    ):
        assert expected in body, expected
