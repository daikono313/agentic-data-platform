"""uv run python -m adp.evals interview [--scenario NAME] [--max-turns N]"""

from __future__ import annotations

import argparse
import sys

import anyio

from adp.catalog.catalog import Catalog
from adp.evals.interview import SCENARIO_DIR, Scenario, default_out_dir, run_scenario, save
from adp.llm import ClaudeAgentRunner, load_settings
from adp.llm.settings import REPO_ROOT


async def _main(args: argparse.Namespace) -> int:
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
    print(f"auth: {settings.auth_mode}, model: {settings.default_model}, out: {out_dir}")
    failed = 0
    for path in paths:
        try:
            result = await run_scenario(
                Scenario.load(path), runner, settings, catalog, args.max_turns
            )
        except Exception as e:  # 1 つのシナリオの失敗で全体を止めない
            print(f"FAIL {path.stem} 例外: {e}")
            failed += 1
            continue
        save(result, out_dir)
        mark = "PASS" if result.passed else "FAIL"
        print(f"{mark} {result.scenario} turns={result.turns} {result.duration_s}s")
        for c in result.checks:
            print(f"  {'ok' if c.passed else 'NG'} {c.name} {'' if c.passed else c.detail}")
        if result.error:
            print(f"  error: {result.error}")
        failed += not result.passed
    print(f"{len(paths) - failed}/{len(paths)} passed")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", choices=["interview"])
    parser.add_argument("--scenario", nargs="*")
    parser.add_argument("--max-turns", type=int, default=25)
    sys.exit(anyio.run(_main, parser.parse_args()))


if __name__ == "__main__":
    main()
