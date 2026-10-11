"""マージ後の自動反映（#74）: 手元が main で変更が無いときだけ取り込み、dbt build と検査をする。"""

import stat
from pathlib import Path

import pytest

from adp.pipeline import StepUpdate
from adp.sync import Sync
from adp.vcs import GitWorkspace
from tests.vcs.test_vcs import git


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(work, "config", k, v)
    (work / "warehouse").mkdir()
    (work / "backend").mkdir()
    (work / "warehouse" / "a.txt").write_text("1")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    # 別の場所で main を進める（GitHub で PR がマージされた状態）
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(other, "config", k, v)
    (other / "warehouse" / "b.txt").write_text("2")
    git(other, "add", ".")
    git(other, "commit", "-m", "merged pr")
    git(other, "push", "origin", "main")
    return work, remote


def _bin(tmp_path: Path, fail: str | None = None) -> Path:
    """dbt / mf / python の代わり（呼ばれた引数を記録し、fail を含む呼び出しだけ失敗する）。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("dbt", "mf", "python"):
        path = bin_dir / name
        code = f'echo "{name} $*" >> {log}\n'
        if fail:
            code += f'case "$*" in *{fail}*) echo "boom"; exit 1;; esac\n'
        path.write_text("#!/bin/sh\n" + code)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_status_reports_behind_and_whether_it_can_pull(tmp_path: Path) -> None:
    work, _ = _repos(tmp_path)
    sync = Sync(work, _bin(tmp_path), GitWorkspace(work))
    status = sync.status()
    assert status.branch == "main" and status.clean and status.behind == 1 and status.can_pull

    (work / "warehouse" / "a.txt").write_text("edited")
    status = sync.status(fetch=False)
    assert not status.can_pull and status.reason and "変更" in status.reason

    git(work, "checkout", "-q", "--", ".")
    git(work, "switch", "-q", "-c", "feat/x")
    status = sync.status(fetch=False)
    assert not status.can_pull and status.reason and "main ではありません" in status.reason


async def test_run_pulls_builds_and_checks(tmp_path: Path) -> None:
    work, _ = _repos(tmp_path)
    updates: list[StepUpdate] = []
    result = await Sync(work, _bin(tmp_path), GitWorkspace(work)).run(updates.append)
    assert result.ok, result.error
    assert (work / "warehouse" / "b.txt").exists()  # 取り込んだ
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert calls[:3] == ["dbt deps", "dbt build", "dbt parse"]
    assert "python -m adp.ontology check" in calls and "python -m adp.cleansing check" in calls
    assert [u.status.value for u in updates if u.step == "pull"][-1] == "done"


async def test_run_does_not_pull_on_a_branch_and_stops_on_failure(tmp_path: Path) -> None:
    work, _ = _repos(tmp_path)
    git(work, "switch", "-q", "-c", "feat/x")
    result = await Sync(work, _bin(tmp_path), GitWorkspace(work)).run()
    assert not result.ok and result.error and "main ではありません" in result.error
    assert not (work / "warehouse" / "b.txt").exists()  # 作業中のブランチは触らない

    git(work, "switch", "-q", "main")
    result = await Sync(work, _bin(tmp_path / "x", fail="build"), GitWorkspace(work)).run()
    assert not result.ok and result.error and "build が失敗しました" in result.error


@pytest.fixture(autouse=True)
def _mkdir(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir(exist_ok=True)
