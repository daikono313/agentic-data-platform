"""S1 を実際の LLM で通しで実行する（#17、MVP の完了条件）。

    uv run python -m adp.evals.e2e [--scenario sales_by_store] [--dry-run]

API と同じサービス層を使い、ユーザー役のエージェントが回答してインタビューを進める。
確定後は本物の生成ジョブ（worktree・dbt・自己修復・push・PR 作成）を実行する。
--dry-run のときは push と PR 作成をせず、生成と検証までで止める。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from adp.api.service import Conflict, SessionService
from adp.api.store import Store
from adp.catalog.catalog import Catalog
from adp.catalog.tools import SERVER_NAME, build_tools
from adp.evals.interview import (
    SCENARIO_DIR,
    Scenario,
    SimulatedUser,
    _check,
    is_transcript_check,
)
from adp.interview import Interviewer
from adp.llm import ClaudeAgentRunner, load_settings
from adp.llm.settings import REPO_ROOT, read_dotenv
from adp.pipeline import GenerationPipeline, PipelineConfig
from adp.vcs import GitHub, GitWorkspace, Worktree


class DryRunGitHub(GitHub):
    def create_pr(
        self, wt: Worktree, title: str, body: str, base: str = "main", draft: bool = False
    ) -> str:
        (REPO_ROOT / ".adp" / "e2e-pr-body.md").write_text(body, encoding="utf-8")
        return f"(dry-run) {wt.branch}"


class DryRunWorkspace(GitWorkspace):
    def push(self, wt: Worktree) -> None:
        return None


async def main(args: argparse.Namespace) -> int:
    settings = load_settings()
    runner = ClaudeAgentRunner(settings)
    db = REPO_ROOT / "warehouse" / "data" / "dev.duckdb"
    catalog = Catalog(db, REPO_ROOT / "warehouse" / "target" / "manifest.json")
    interviewer = Interviewer(runner, settings, {SERVER_NAME: build_tools(catalog)})
    account = (
        read_dotenv(REPO_ROOT / ".env").get("ADP_GITHUB_ACCOUNT")
        or GitWorkspace(REPO_ROOT).remote_owner()
    )
    workspace = DryRunWorkspace(REPO_ROOT) if args.dry_run else GitWorkspace(REPO_ROOT)
    github = DryRunGitHub(REPO_ROOT, account) if args.dry_run else GitHub(REPO_ROOT, account)
    pipeline = GenerationPipeline(
        PipelineConfig(REPO_ROOT, Path(sys.executable).parent, db, account),
        runner,
        settings,
        workspace,
        github,
    )
    # 開発サーバーの app.db とは分ける（起動時に実行中のジョブを失敗扱いにするため）
    service = SessionService(Store(REPO_ROOT / ".adp" / "e2e.db"), interviewer, pipeline.run)
    scenario = Scenario.load(SCENARIO_DIR / f"{args.scenario}.yml")
    user = SimulatedUser(runner, settings, scenario.persona)
    print(f"auth: {settings.auth_mode}, account: {account}, dry-run: {args.dry_run}")

    try:
        return await _run(args, service, scenario, user, workspace)
    except Conflict as e:
        print(f"エラー: {e}")
        return 1


async def _run(
    args: argparse.Namespace,
    service: SessionService,
    scenario: Scenario,
    user: SimulatedUser,
    workspace: GitWorkspace,
) -> int:
    sid = service.create(scenario.request)
    message = scenario.request
    for turn in range(1, args.max_turns + 1):
        reply, choices, error = "", [], None
        async for event in service.send(sid, message):
            data = json.loads(event["data"])
            if event["event"] == "choices":
                choices = data["options"]
            elif event["event"] == "done":
                reply, error = data["message"]["text"], data["error"]
                # 画面と同じく、ユーザー役には本文の後ろに質問文が見える
                if data["message"].get("question"):
                    reply = f"{reply}\n\n{data['message']['question']}".strip()
        print(f"[{turn}] user: {message}\n    agent: {reply[:300]}")
        if error:
            print(f"エラー: {error}")
            return 1
        view = service.view(sid)
        if view["phase"] == "review" and not view["issues"]:
            answer = await user.reply(reply, choices)
            if answer.strip().startswith("確定します"):
                break
            message = answer
            continue
        message = await user.reply(reply, choices)
    else:
        print("インタビューが終わりませんでした")
        return 1

    # インタビューの成果物を eval と同じ基準で採点してから確定する
    state = service._state(sid)
    results = [
        _check(state.beam, c)
        for c in scenario.checks
        if c != "finalizable" and not is_transcript_check(c)  # E2E は BEAM 成果物だけを見る
    ]
    for r in results:
        print(f"  {'ok' if r.passed else 'NG'} {r.name} {'' if r.passed else r.detail}")
    if not all(r.passed for r in results):
        print("インタビューの成果物が要件を満たしていません")
        return 1

    final = service.finalize(sid)
    job_id = final["job"]["id"]
    branch: str | None = None
    print(f"確定しました（job {job_id}）")
    async for event in service.job_events(job_id):
        data = json.loads(event["data"])
        if event["event"] == "step":
            print(f"  {data['status']:8} {data['label']} {data['detail'][:200]}")
            if data["step"] == "worktree" and data["status"] == "done":
                branch = data["detail"]
        elif event["event"] == "done":
            print(f"結果: {data['status']}  PR: {data['pr_url']}  {data['error'] or ''}")
            if args.dry_run and branch:  # dry-run は push しないので、作業ブランチを残さない
                wt = workspace.worktrees_dir / branch.replace("/", "-")
                if wt.exists():
                    workspace.remove_worktree(Worktree(wt, branch), delete_branch=True)
            return 0 if data["status"] == "succeeded" else 1
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="sales_by_store")
    parser.add_argument("--max-turns", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args())))
