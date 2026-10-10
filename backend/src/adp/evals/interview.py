"""インタビューの eval。

    uv run python -m adp.evals interview [--scenario NAME] [--max-turns N]

ユーザー役（シミュレーター）は、シナリオの persona だけを知っている別のエージェント。
インタビューが review 段階に達し確定条件を満たしたら確定し、checks で採点する。
結果は `.adp/evals/<日時>/<シナリオ>.json` に保存する。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from adp.beam import Aggregation, Availability, BeamModel, GapKind, MetricType, W
from adp.catalog.catalog import Catalog
from adp.catalog.tools import SERVER_NAME as CATALOG_SERVER
from adp.catalog.tools import build_tools as catalog_tools
from adp.interview import ChoicesPresented, Interviewer, Phase, TurnDone, new_state
from adp.llm import AgentRunner, AgentSpec, LLMSettings, TextDelta, ToolCall, TurnResult
from adp.llm.settings import REPO_ROOT

SCENARIO_DIR = REPO_ROOT / "evals" / "interview"

USER_PROMPT = """\
あなたはデータ分析の要件ヒアリングを受けているビジネスユーザーを演じます。
以下のペルソナの知っていることだけをもとに、聞かれたことに短く（1〜3 文で）答えてください。
選択肢が示されたら、ペルソナに合うものを選んでそのまま答えてください。
ペルソナに書かれていないことを聞かれたら、常識的な範囲で答えるか「おまかせします」と答えてください。
要約を示されて確定を求められ、内容が要望どおりなら「確定します」とだけ答えてください。
要望と違う点があれば、確定せずにその点を伝えてください。

# ペルソナ
{persona}
"""


@dataclass
class Scenario:
    name: str
    request: str
    persona: str
    checks: list[Any]

    @classmethod
    def load(cls, path: Path) -> Scenario:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(**data)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class EvalResult:
    scenario: str
    passed: bool
    checks: list[CheckResult]
    turns: int
    duration_s: float
    first_token_s: list[float] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[list[dict[str, Any]]] = field(default_factory=list)
    """ターンごとのエージェントの tool 呼び出し（CI での再生テストに使う）。"""
    beam: dict[str, Any] | None = None
    error: str | None = None


def _metrics_events(beam: BeamModel) -> list[tuple[Any, Any]]:
    return [(m, beam.event(m.event)) for m in beam.metrics]


def _check(beam: BeamModel, spec: Any) -> CheckResult:
    name, raw = (spec, "") if isinstance(spec, str) else next(iter(spec.items()))
    args: list[str] = [str(a) for a in raw] if isinstance(raw, list) else []
    arg = "" if isinstance(raw, list) else str(raw)
    label = f"{name}: {raw}" if raw else name
    checks: dict[str, Callable[[], tuple[bool, str]]] = {
        "finalizable": lambda: (beam.finalized_at is not None, "; ".join(beam.finalize_issues())),
        "has_w": lambda: (
            all(any(d.w == W(w) for e in beam.events for d in e.dimensions) for w in args),
            str(sorted({d.w.value for e in beam.events for d in e.dimensions})),
        ),
        "metric_group_by_w": lambda: (
            any(
                e is not None
                and any(
                    (d := e.dimension(ref.partition(".")[0])) is not None and d.w == W(arg)
                    for ref in m.group_by
                )
                for m, e in _metrics_events(beam)
            ),
            str([m.group_by for m in beam.metrics]),
        ),
        "has_gap": lambda: (
            any(g.kind == GapKind(arg) for g in beam.gaps),
            str([g.kind.value for g in beam.gaps]),
        ),
        "metric_has_filter": lambda: (
            any(m.filters for m in beam.metrics),
            str([[f.model_dump(mode="json") for f in m.filters] for m in beam.metrics]),
        ),
        "has_examples": lambda: (
            any(e.examples for e in beam.events),
            str([len(e.examples) for e in beam.events]),
        ),
        "metric_type": lambda: (
            any(m.type == MetricType(arg) for m in beam.metrics),
            str([m.type.value for m in beam.metrics]),
        ),
        "metric_aggregation": lambda: (
            any(
                e is not None
                and m.measure is not None
                and (ms := e.measure(m.measure)) is not None
                and ms.aggregation == Aggregation(arg)
                and ms.availability == Availability.AVAILABLE
                for m, e in _metrics_events(beam)
            ),
            str([m.measure for m in beam.metrics]),
        ),
        "min_events": lambda: (len(beam.events) >= int(arg), str(len(beam.events))),
    }
    passed, detail = checks[name]()
    return CheckResult(label, passed, detail)


class SimulatedUser:
    def __init__(self, runner: AgentRunner, settings: LLMSettings, persona: str) -> None:
        self.runner = runner
        self.spec = AgentSpec(
            purpose="eval-user",
            system_prompt=USER_PROMPT.format(persona=persona),
            model=settings.default_model,
            max_turns=1,
        )
        self.session: str | None = None

    async def reply(self, message: str, choices: list[str]) -> str:
        prompt = message + (f"\n\n選択肢: {' / '.join(choices)}" if choices else "")
        text: list[str] = []
        async for event in self.runner.run(prompt, self.spec, resume=self.session):
            if isinstance(event, TextDelta):
                text.append(event.text)
            elif isinstance(event, TurnResult):
                self.session = event.session_id or self.session
        return "".join(text).strip() or "おまかせします"


async def run_scenario(
    scenario: Scenario,
    runner: AgentRunner,
    settings: LLMSettings,
    catalog: Catalog,
    max_turns: int = 25,
    log: Callable[[str], None] = print,
) -> EvalResult:
    interviewer = Interviewer(runner, settings, {CATALOG_SERVER: catalog_tools(catalog)})
    user = SimulatedUser(runner, settings, scenario.persona)
    state = new_state(scenario.request)
    start = time.monotonic()
    first_tokens: list[float] = []
    message = scenario.request
    error: str | None = None
    turns = 0
    tool_calls: list[list[dict[str, Any]]] = []
    while turns < max_turns:
        turns += 1
        log(f"[{scenario.name}] user: {message}")
        reply = ""
        choices: list[str] = []
        calls: list[dict[str, Any]] = []
        tool_calls.append(calls)
        async for event in interviewer.send(state, message):
            if isinstance(event, ToolCall):
                calls.append({"name": event.name, "input": event.input})
            elif isinstance(event, ChoicesPresented):
                choices = event.options
            elif isinstance(event, TurnDone):
                reply = event.message.text
                if event.result.first_token_s is not None:
                    first_tokens.append(event.result.first_token_s)
                if event.result.is_error:
                    error = event.result.error
        log(f"[{scenario.name}] agent ({state.phase}): {reply}")
        if error:
            break
        if state.phase == Phase.REVIEW and not state.beam.finalize_issues():
            answer = await user.reply(reply, choices)
            log(f"[{scenario.name}] user: {answer}")
            if answer.strip().startswith("確定します"):
                interviewer.finalize(state)
                break
            message = answer
            continue
        message = await user.reply(reply, choices)

    checks = [_check(state.beam, c) for c in scenario.checks]
    return EvalResult(
        scenario=scenario.name,
        passed=error is None and all(c.passed for c in checks),
        checks=checks,
        turns=turns,
        duration_s=round(time.monotonic() - start, 1),
        first_token_s=[round(t, 2) for t in first_tokens],
        transcript=[{"role": m.role, "text": m.text} for m in state.messages],
        tool_calls=tool_calls,
        beam=state.beam.model_dump(mode="json"),
        error=error,
    )


def save(result: EvalResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.scenario}.json"
    data = {
        **{k: v for k, v in result.__dict__.items() if k != "checks"},
        "checks": [c.__dict__ for c in result.checks],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def default_out_dir() -> Path:
    return REPO_ROOT / ".adp" / "evals" / datetime.now().strftime("%Y%m%d-%H%M%S")
