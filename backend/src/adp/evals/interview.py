"""インタビューの eval。

    uv run python -m adp.evals interview [--scenario NAME] [--max-turns N]

ユーザー役（シミュレーター）は、シナリオの persona だけを知っている別のエージェント。
インタビューが review 段階に達し確定条件を満たしたら確定し、checks で採点する。
結果は `.adp/evals/<日時>/<シナリオ>.json` に保存する。
"""

from __future__ import annotations

import difflib
import json
import re
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
from adp.design.check import design_issues
from adp.interview import ChoicesPresented, Interviewer, Phase, TurnDone, new_state
from adp.interview.prompts import prompt_version
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
    metrics: dict[str, float] = field(default_factory=dict)
    """会話の質の数値（transcript_metrics）。"""
    prompt_version: str | None = None


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


_POSITION_WORDS = ("上の選択肢", "下の選択肢", "上のボタンから", "下のボタンから", "ボタンから選")
_PUNCT = re.compile(r"[\s、。，．,.!！?？「」『』（）()]")


def _normalize(text: str) -> str:
    return _PUNCT.sub("", text)


# 回答者（データの専門家ではない）にそのまま使うとわかりにくい言葉
# （#45。フィードバックの分類「専門用語がわからない」）。英語は大文字・小文字を区別せず単語で探す
JARGON = (
    "粒度",
    "グレイン",
    "ディメンション",
    "ファクト",
    "メジャー",
    "スタースキーマ",
    "コンフォーム",
    "縮退",
    "エンティティ",
    "カーディナリティ",
    "SCD",
    "BEAM",
    "7W",
    "grain",
    "dimension",
    "fact table",
    "measure",
)
# 用語の後（複合語の続き「テーブル」なども許す）に括弧・「とは」などがあれば説明済み
_EXPLAINED_AFTER = re.compile(r"^[ァ-ヶー]{0,6}\s*(?:[（(]|とは|＝|=|:|：)")
# 「1 行が何を表すか（粒度）」のように、説明の後に括弧で添えた場合も説明済み
_EXPLAINED_BEFORE = re.compile(r"[（(]\s*$")
_QUESTION = re.compile(r"[^。\n？?]*[？?]")
DUPLICATE_RATIO = 0.95
"""同じ質問とみなす類似度（「顧客について…」「商品について…」のような型の同じ別の質問は除く）。"""
_CONFIRM_WORDS = ("確定", "よろしいですか", "問題ありませんか")


def _term_pattern(term: str) -> re.Pattern[str]:
    if term.isascii():
        return re.compile(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", re.IGNORECASE)
    return re.compile(re.escape(term))


def _unexplained_jargon(messages: list[Any]) -> list[str]:
    """説明なしで使った専門用語（会話で最初に使ったときに、括弧などで説明していないもの）。"""
    text = "\n".join(_shown(m) for m in messages if m.role == "assistant")
    found = []
    for term in JARGON:
        m = _term_pattern(term).search(text)
        if m is None:
            continue
        after = text[m.end() : m.end() + 12]
        before = text[max(0, m.start() - 3) : m.start()]
        if not (_EXPLAINED_AFTER.match(after) or _EXPLAINED_BEFORE.search(before)):
            found.append(term)
    return found


def _shown(m: Any) -> str:
    """画面に出る文（本文と、ボタンの上の質問文）。"""
    return f"{m.text}\n{m.question}" if getattr(m, "question", None) else str(m.text)


def _repeated_questions(messages: list[Any]) -> list[str]:
    """別の発言で、同じ質問をもう一度した（フィードバックの「質問が重複している」）。

    ユーザーが聞き返した直後の言い直しと、確定の確認（修正のたびに聞き直す）は数えない。
    """
    questions: list[tuple[int, str, str, bool]] = []
    previous_user = ""
    n = -1
    for m in messages:
        if m.role != "assistant":
            previous_user = str(m.text)
            continue
        n += 1
        asked_back = "？" in previous_user or "?" in previous_user
        for q in _QUESTION.findall(_shown(m)):
            normalized = _normalize(q)
            if len(normalized) >= 8:
                exempt = asked_back or any(w in q for w in _CONFIRM_WORDS)
                questions.append((n, normalized, q.strip(), exempt))
    repeated = []
    for a in range(len(questions)):
        for b in range(a + 1, len(questions)):
            if questions[a][0] == questions[b][0] or questions[b][3]:
                continue
            ratio = difflib.SequenceMatcher(None, questions[a][1], questions[b][1]).ratio()
            if ratio >= DUPLICATE_RATIO:
                repeated.append(questions[b][2])
    return repeated


def transcript_metrics(messages: list[Any]) -> dict[str, float]:
    """会話の質の数値（前後比較に使う。#45）。"""
    assistant = [m for m in messages if m.role == "assistant"]
    lengths = [len(_shown(m)) for m in assistant] or [0]
    return {
        "assistant_turns": len(assistant),
        "questions": sum(1 for m in assistant if _QUESTION.search(_shown(m))),
        "avg_chars": round(sum(lengths) / len(lengths), 1),
        "max_chars": max(lengths),
        "unexplained_jargon": len(_unexplained_jargon(messages)),
        "repeated_questions": len(_repeated_questions(messages)),
        "position_words": sum(1 for m in assistant if any(w in m.text for w in _POSITION_WORDS)),
    }


def _transcript_check_map(messages: list[Any]) -> dict[str, CheckResult]:
    assistant = [m for m in messages if m.role == "assistant"]
    position = [m.text[:60] for m in assistant if any(w in m.text for w in _POSITION_WORDS)]
    repeated = [
        m.text[:60]
        for m in assistant
        if m.question and _normalize(m.question) and _normalize(m.question) in _normalize(m.text)
    ]
    jargon = _unexplained_jargon(messages)
    again = _repeated_questions(messages)
    results = [
        CheckResult("no_choice_position_words", not position, " / ".join(position[:3])),
        CheckResult("question_not_repeated_in_text", not repeated, " / ".join(repeated[:3])),
        CheckResult("no_unexplained_jargon", not jargon, ", ".join(jargon)),
        CheckResult("no_repeated_questions", not again, " / ".join(again[:3])),
    ]
    return {r.name: r for r in results}


TRANSCRIPT_CHECKS = (
    "no_choice_position_words",
    "question_not_repeated_in_text",
    "no_unexplained_jargon",
    "no_repeated_questions",
)
TRANSCRIPT_LIMITS = {"max_assistant_turns": "assistant_turns", "max_assistant_chars": "max_chars"}


def _transcript_checks(messages: list[Any]) -> list[CheckResult]:
    """会話の質のチェック（#36、#45）。どのシナリオでも行う。"""
    return list(_transcript_check_map(messages).values())


def is_transcript_check(spec: Any) -> bool:
    """会話を見るチェックか（BEAM 成果物だけを見る再生テストや E2E では行わない）。"""
    name = spec if isinstance(spec, str) else next(iter(spec))
    return name in TRANSCRIPT_CHECKS or name in TRANSCRIPT_LIMITS


def _scenario_transcript_check(spec: Any, messages: list[Any]) -> CheckResult | None:
    """会話を見るチェック（シナリオに書いたもの）。BEAM のチェックなら None。"""
    if not is_transcript_check(spec):
        return None
    if isinstance(spec, str):
        return _transcript_check_map(messages)[spec]
    name, raw = next(iter(spec.items()))
    value = transcript_metrics(messages)[TRANSCRIPT_LIMITS[name]]
    return CheckResult(f"{name}: {raw}", value <= int(raw), str(value))


def run_check(beam: BeamModel, messages: list[Any], spec: Any) -> CheckResult:
    """シナリオの 1 つのチェック（BEAM 成果物か会話のどちらかを見る）。"""
    return _scenario_transcript_check(spec, messages) or _check(beam, spec)


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
    interviewer = Interviewer(
        runner,
        settings,
        {CATALOG_SERVER: catalog_tools(catalog)},
        design_issues=design_issues(catalog, REPO_ROOT / "ontology" / "ontology.yml"),
    )
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
        # 画面と同じく、ユーザー役には本文の後ろに質問文が見える
        if state.messages and state.messages[-1].question:
            reply = f"{reply}\n\n{state.messages[-1].question}".strip()
        if error:
            break
        if state.phase == Phase.REVIEW and not interviewer.issues(state.beam):
            answer = await user.reply(reply, choices)
            log(f"[{scenario.name}] user: {answer}")
            if answer.strip().startswith("確定します"):
                interviewer.finalize(state)
                break
            message = answer
            continue
        message = await user.reply(reply, choices)

    checks = [run_check(state.beam, state.messages, c) for c in scenario.checks]
    named = {c.name for c in checks}
    checks += [c for c in _transcript_checks(state.messages) if c.name not in named]
    return EvalResult(
        scenario=scenario.name,
        passed=error is None and all(c.passed for c in checks),
        checks=checks,
        turns=turns,
        duration_s=round(time.monotonic() - start, 1),
        first_token_s=[round(t, 2) for t in first_tokens],
        transcript=[
            {"role": m.role, "text": m.text, **({"question": m.question} if m.question else {})}
            for m in state.messages
        ],
        metrics=transcript_metrics(state.messages),
        prompt_version=prompt_version(),
        tool_calls=tool_calls,
        beam=state.beam.model_dump(mode="json"),
        error=error,
    )


def save(result: EvalResult, out_dir: Path, suffix: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.scenario}{suffix}.json"
    data = {
        **{k: v for k, v in result.__dict__.items() if k != "checks"},
        "checks": [c.__dict__ for c in result.checks],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def default_out_dir() -> Path:
    return REPO_ROOT / ".adp" / "evals" / datetime.now().strftime("%Y%m%d-%H%M%S")
