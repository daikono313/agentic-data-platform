"""eval とフィードバックの改善サイクル（#45）。

uv run python -m adp.evals interview [--scenario NAME ...] [--max-turns N] [--repeat N]
uv run python -m adp.evals compare <前の結果> <後の結果>
uv run python -m adp.evals feedback-report [--summary]
uv run python -m adp.evals feedback-drafts
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import anyio

from adp.llm.settings import REPO_ROOT

APP_DB = REPO_ROOT / ".adp" / "app.db"


async def _interview(args: argparse.Namespace) -> int:
    from adp.catalog.catalog import Catalog
    from adp.evals.interview import (
        SCENARIO_DIR,
        EvalResult,
        Scenario,
        default_out_dir,
        run_scenario,
        save,
    )
    from adp.interview.prompts import prompt_version
    from adp.llm import ClaudeAgentRunner, load_settings

    settings = load_settings()
    runner = ClaudeAgentRunner(settings)
    catalog = Catalog(
        REPO_ROOT / "warehouse" / "data" / "dev.duckdb",
        REPO_ROOT / "warehouse" / "target" / "manifest.json",
    )
    paths = sorted(SCENARIO_DIR.glob("*.yml"))
    if args.scenario:
        paths = [p for p in paths if p.stem in args.scenario]
    out_dir = default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "prompt_version": prompt_version(),
        "model": settings.default_model,
        "at": datetime.now().isoformat(timespec="seconds"),
        "repeat": args.repeat,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"auth: {settings.auth_mode}, model: {settings.default_model}, out: {out_dir}")
    print(f"prompt: {meta['prompt_version']}")
    failed = total = 0
    for path in paths:
        for i in range(args.repeat):
            total += 1
            name = path.stem if args.repeat == 1 else f"{path.stem} #{i + 1}"
            try:
                result = await run_scenario(
                    Scenario.load(path), runner, settings, catalog, args.max_turns
                )
            except Exception as e:  # 1 つのシナリオの失敗で全体を止めない
                print(f"FAIL {name} 例外: {e}")
                failed += 1
                # 前後比較で合格率を水増ししないよう、失敗として残す
                save(
                    EvalResult(path.stem, False, [], 0, 0.0, error=f"例外: {e}"),
                    out_dir,
                    suffix="" if args.repeat == 1 else f".{i + 1}",
                )
                continue
            save(result, out_dir, suffix="" if args.repeat == 1 else f".{i + 1}")
            mark = "PASS" if result.passed else "FAIL"
            print(f"{mark} {name} turns={result.turns} {result.duration_s}s {result.metrics}")
            for c in result.checks:
                print(f"  {'ok' if c.passed else 'NG'} {c.name} {'' if c.passed else c.detail}")
            if result.error:
                print(f"  error: {result.error}")
            failed += not result.passed
    print(f"{total - failed}/{total} passed")
    return 1 if failed else 0


def _compare(args: argparse.Namespace) -> int:
    from adp.evals.compare import compare

    print(compare(Path(args.before), Path(args.after)))
    return 0


async def _feedback_report(args: argparse.Namespace) -> int:
    from adp.api.store import Store
    from adp.evals.feedback import report, summarize

    if not APP_DB.exists():
        print(f"{APP_DB} がありません（まだフィードバックがありません）")
        return 0
    rows = Store(APP_DB, feedback_retention_days=0).feedback()
    text = report(rows)
    if args.summary and not rows:
        print("フィードバックが無いので、Claude による要約は省きます")
    elif args.summary:
        from adp.llm import ClaudeAgentRunner, load_settings

        settings = load_settings()
        text += "\n## Claude による要約と改善案\n\n"
        text += await summarize(text, ClaudeAgentRunner(settings), settings) + "\n"
    out = REPO_ROOT / ".adp" / "reports" / f"feedback-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"（保存: {out.relative_to(REPO_ROOT)}）")
    return 0


def _feedback_drafts(args: argparse.Namespace) -> int:
    from adp.api.store import Store
    from adp.evals.feedback import drafts, no_drafts_hint

    if not APP_DB.exists():
        print(f"{APP_DB} がありません（まだフィードバックがありません）")
        return 0
    store = Store(APP_DB, feedback_retention_days=0)
    paths = drafts(store, REPO_ROOT / ".adp" / "eval-drafts")
    if not paths:
        print(no_drafts_hint(store.feedback()))
        return 0
    for p in paths:
        print(p.relative_to(REPO_ROOT))
    print(
        f"{len(paths)} 件の下書きを作りました。実データを含むので、request と persona を"
        "合成した内容に書き換えてから evals/interview/ に置いてください"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("interview")
    p.add_argument("--scenario", nargs="*")
    p.add_argument("--max-turns", type=int, default=25)
    p.add_argument("--repeat", type=int, default=1, help="LLM のぶれを見るため、各シナリオを N 回")
    p = sub.add_parser("compare")
    p.add_argument("before")
    p.add_argument("after")
    p = sub.add_parser("feedback-report")
    p.add_argument("--summary", action="store_true", help="Claude に要約と改善案を出させる")
    sub.add_parser("feedback-drafts")
    args = parser.parse_args()
    if args.command == "interview":
        sys.exit(anyio.run(_interview, args))
    if args.command == "compare":
        sys.exit(_compare(args))
    if args.command == "feedback-report":
        sys.exit(anyio.run(_feedback_report, args))
    sys.exit(_feedback_drafts(args))


if __name__ == "__main__":
    main()
