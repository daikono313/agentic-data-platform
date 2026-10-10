"""S1（と v2 の S3・S4）を実際の LLM で通しで実行する（#17、#46）。

    uv run python -m adp.evals.e2e [--scenario sales_by_store] [--dry-run] [--v2]

--v2 のときは、データ一覧を見てから要求し、インタビュー中に最初の発言へフィードバックし、
確定後に全体を評価し、生成した指標を描画画面（PR の内容でプレビュー）で描き、フィードバックの
集計に出ることまで確かめる（design-doc の S3 と S4 の 1〜2）。

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
from typing import Any

from adp.api.service import Conflict, SessionService
from adp.api.store import Store
from adp.catalog.catalog import Catalog
from adp.catalog.tools import SERVER_NAME, build_tools
from adp.design.check import design_issues
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
    interviewer = Interviewer(
        runner, settings, {SERVER_NAME: build_tools(catalog)}, design_issues=design_issues(catalog)
    )
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
    if args.v2:  # S3-1: 開始画面で使えるデータを見る
        from adp.datasets import DatasetIndex

        datasets = DatasetIndex(REPO_ROOT, None).all()
        print(
            f"使えるデータ: 指標 {len(datasets.metrics)}・モデル {len(datasets.models)}・"
            f"BEAM {len(datasets.beams)}"
            + (f"（{' / '.join(datasets.errors)}）" if datasets.errors else "")
        )
    sid = service.create(scenario.request)
    message = scenario.request
    feedback_sent = False
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
        if args.v2 and not feedback_sent:  # S4-1: インタビュー中に質問へフィードバックする
            from adp.feedback import FeedbackIn

            index = len(service._state(sid).messages) - 1
            saved = service.feedback(
                sid,
                FeedbackIn(
                    kind="message",
                    message_index=index,
                    thumb="down",
                    categories=["jargon"],
                    comment="（E2E）『粒度』の意味がわからない",
                ),
            )
            print(f"  フィードバック: {index} 番目に 👎（{saved['prompt_version']}）")
            feedback_sent = True
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
    result: dict[str, Any] | None = None
    print(f"確定しました（job {job_id}）")
    async for event in service.job_events(job_id):
        data = json.loads(event["data"])
        if event["event"] == "step":
            print(f"  {data['status']:8} {data['label']} {data['detail'][:200]}")
            if data["step"] == "worktree" and data["status"] == "done":
                branch = data["detail"]
        elif event["event"] == "done":
            print(f"結果: {data['status']}  PR: {data['pr_url']}  {data['error'] or ''}")
            result = data
            break
    ok = result is not None and result["status"] == "succeeded"
    try:
        if ok and args.v2:
            ok = _v2_after_pr(service, sid, job_id, service._state(sid).beam.metrics)
    finally:
        if args.dry_run and branch:  # dry-run は push しないので、作業ブランチを残さない
            wt = workspace.worktrees_dir / branch.replace("/", "-")
            if wt.exists():
                workspace.remove_worktree(Worktree(wt, branch), delete_branch=True)
    return 0 if ok else 1


def _v2_after_pr(service: SessionService, sid: str, job_id: str, metrics: list[Any]) -> bool:
    """S3-4 と S4: 全体の評価 → 描画画面（PR の内容でプレビュー）→ フィードバックの集計。"""
    from adp.chart_view import ChartRequest, ChartViews
    from adp.evals.feedback import report
    from adp.feedback import FeedbackIn

    service.feedback(sid, FeedbackIn(kind="session", score=4, comment="（E2E）全体の評価"))
    branch = service.job_branch(job_id)
    if branch is None:
        print("描画: PR のブランチがありません")
        return False
    view = ChartViews(REPO_ROOT).for_branch(branch)
    for m in metrics:
        info = next((x for x in view.metrics()[0] if x.name == m.name), None)
        if info is None:
            print(f"描画: 指標 {m.name} がありません")
            return False
        dims = [d.name for d in info.dimensions if d.kind == "categorical"]
        # 要求で使った切り口（例: product.category → product__category）を優先する
        wanted = [g.replace(".", "__") for g in m.group_by]
        dims = [d for d in dims if d in wanted] + [d for d in dims if d not in wanted]
        for req in (
            ChartRequest(metric=m.name, grain="month"),
            ChartRequest(metric=m.name, dimension=dims[0] if dims else None, grain="week"),
        ):
            try:
                chart = view.render(req)
            except Exception as e:
                print(f"  描画できませんでした（{req.model_dump(exclude_none=True)}）: {e}")
                return False
            print(f"  描画 {chart.kind}: {chart.title}（SVG {len(chart.svg):,} 文字）")
    text = report(service.store.feedback(sid))
    found = "（E2E）『粒度』の意味がわからない" in text and "平均 4.0 点" in text
    print(f"フィードバックの集計: {'出ている' if found else '出ていない'}")
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="sales_by_store")
    parser.add_argument("--max-turns", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--v2", action="store_true", help="S3・S4（データ一覧・描画・フィードバック）"
    )
    sys.exit(asyncio.run(main(parser.parse_args())))
