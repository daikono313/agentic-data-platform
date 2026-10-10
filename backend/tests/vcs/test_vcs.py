import subprocess
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from adp.beam import load
from adp.design import DesignBuilder
from adp.generation import AgentTask
from adp.vcs import (
    ChartPreview,
    GitHub,
    GitWorkspace,
    Verification,
    branch_name,
    pr_body,
    pr_title,
)
from adp.vcs.git import GitError, Worktree
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


@pytest.mark.parametrize(
    ("out", "expected"),
    [
        ("daikono313/auto_data_platform\n", "daikono313/auto_data_platform"),
        ("", None),
        ("a/b)](https://evil", None),
    ],
)
def test_repo_name_from_gh(out: str, expected: str | None) -> None:
    def runner(
        args: Sequence[str], cwd: Path, env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "tok" if "auth" in args else out, "")

    assert GitHub(Path("."), "daikono313", runner).repo_name(Worktree(Path("."), "b")) == expected


def test_branch_name() -> None:
    assert branch_name("sales", "customer_orders_product", date(2026, 10, 10)) == (
        "beam/customer_orders_product-20261010"
    )


def test_worktree_commit_push(repo: Path) -> None:
    ws = GitWorkspace(repo)
    assert ws.remote_owner() == "daikono313"
    (repo / "warehouse").mkdir()
    (repo / "warehouse" / "dim.yml").write_text("old")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "dim")
    git(repo, "push")
    wt = ws.create_worktree("beam/x-20261010")
    assert wt.path == repo / ".adp" / "worktrees" / "beam-x-20261010"
    # 既存ファイルの変更（status の先頭行になる）・空白を含む新規ファイル・対象外のファイル
    (wt.path / "warehouse" / "dim.yml").write_text("new")
    (wt.path / "warehouse" / "new file.sql").write_text("select 1")
    (wt.path / "other.txt").write_text("not committed")
    files = ws.stage(wt, ["warehouse", "beam"])
    assert files == ["warehouse/dim.yml", "warehouse/new file.sql"]
    sha = ws.commit(wt, "feat: a")
    ws.push(wt)
    assert git(repo, "ls-remote", "origin", "beam/x-20261010").startswith(sha)
    assert "other.txt" in git(wt.path, "status", "--porcelain")

    # 同じ名前は -2 を付ける（ローカルの worktree を消しても、リモートにあれば避ける）
    ws.remove_worktree(wt, delete_branch=True)
    assert not wt.path.exists()
    again = ws.create_worktree("beam/x-20261010")
    assert again.branch == "beam/x-20261010-2"


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
    assert "## チャート" not in body


def test_pr_body_shows_charts() -> None:
    beam = load(FIXTURE)
    design = DesignBuilder(FakeSchema()).build(beam)
    preview = ChartPreview(
        "warehouse/chart_specs/sales_by_store.yml",
        [("売上金額 #1", "line")],
        image_url="https://github.com/o/r/blob/abc/pr_assets/sales_by_store.png?raw=true",
    )
    body = pr_body(beam, design, [], Verification(True), chart=preview)
    assert "## チャート" in body and "（折れ線）" in body and "#1" not in body
    assert (
        "![チャート](https://github.com/o/r/blob/abc/pr_assets/sales_by_store.png?raw=true)" in body
    )
    failed = ChartPreview("warehouse/chart_specs/x.yml", error="描画できませんでした")
    body = pr_body(beam, design, [], Verification(True), chart=failed)
    assert "❌ チャートを作れませんでした: 描画できませんでした" in body and "![" not in body


def test_pr_body_is_safe_for_public_repo() -> None:
    from adp.vcs.pr_body import _safe

    text = _safe("Closes #12 cc @someone /Users/me/secret/path.txt ```x```")
    assert "#12" not in text and "@someone" not in text
    assert "/Users/me" not in text and "<path>" in text
    assert "```" not in text


def test_long_body_uses_file_and_is_truncated(repo: Path) -> None:
    seen: dict[str, str] = {}

    def fake(
        args: Sequence[str], cwd: Path, env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        args = list(args)
        if "--body-file" in args:
            seen["body"] = Path(args[args.index("--body-file") + 1]).read_text()
        out = "tok" if args[:3] == ["gh", "auth", "token"] else "https://x/pull/1"
        return subprocess.CompletedProcess(args, 0, out, "")

    from adp.vcs import Worktree
    from adp.vcs.git import MAX_BODY

    GitHub(repo, "daikono313", fake).create_pr(Worktree(repo, "b"), "t", "x" * 100000)
    assert len(seen["body"]) <= MAX_BODY and "省略" in seen["body"]
