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

from adp.beam import (
    Additivity,
    Aggregation,
    Availability,
    BeamModel,
    BusinessEvent,
    FilterOperator,
    GapKind,
    MetricType,
    SourceRef,
    StoryType,
    W,
)
from adp.beam.validation import resolve_ref
from adp.catalog.catalog import Catalog
from adp.catalog.tools import SERVER_NAME as CATALOG_SERVER
from adp.catalog.tools import build_tools as catalog_tools
from adp.design import ForeignKey, SchemaInfo
from adp.design.builder import base_table
from adp.design.catalog_schema import CatalogSchema
from adp.design.check import design_issues, load_ontology
from adp.interview import ChoicesPresented, Interviewer, Phase, TurnDone, new_state
from adp.interview.prompts import prompt_version
from adp.interview.wording import (
    has_example_values,
    is_closing_sentence,
    is_example_confirmation,
    is_position_sentence,
)
from adp.llm import AgentRunner, AgentSpec, LLMSettings, TextDelta, ToolCall, TurnResult
from adp.llm.settings import REPO_ROOT
from adp.warehouse import warehouse_db

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
    onboard: str | None = None
    """まだオントロジーに無い源泉（例: raw.reviews）。eval ではプロファイルからの決定的な追加案で
    取り込んだものとして設計を確かめ、E2E（--v3）では本物の取り込み（エージェント → PR）をする。"""

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
    transcript: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[list[dict[str, Any]]] = field(default_factory=list)
    """ターンごとのエージェントの tool 呼び出し（CI での再生テストに使う）。"""
    beam: dict[str, Any] | None = None
    error: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    """会話の質の数値（transcript_metrics）。"""
    prompt_version: str | None = None


def _metrics_events(beam: BeamModel) -> list[tuple[Any, Any]]:
    return [(m, beam.event(m.event)) for m in beam.metrics]


def _metric_event_list(beam: BeamModel) -> list[Any]:
    """指標の出来事（重複なし）。"""
    names = dict.fromkeys(m.event for m in beam.metrics)
    return [e for n in names if (e := beam.event(n)) is not None]


def _check(beam: BeamModel, spec: Any) -> CheckResult:
    name, raw = (spec, "") if isinstance(spec, str) else next(iter(spec.items()))
    args: list[str] = [str(a) for a in raw] if isinstance(raw, list) else []
    arg = "" if isinstance(raw, list | dict) else str(raw)
    opts: dict[str, Any] = raw if isinstance(raw, dict) else {}
    label = f"{name}: {raw}" if raw else name
    checks: dict[str, Callable[[], tuple[bool, str]]] = {
        "finalizable": lambda: (beam.finalized_at is not None, "; ".join(beam.finalize_issues())),
        "has_w": lambda: (
            all(any(d.w == W(w) for e in beam.events for d in e.dimensions) for w in args),
            str(sorted({d.w.value for e in beam.events for d in e.dimensions})),
        ),
        "has_gap": lambda: (
            any(g.kind == GapKind(arg) for g in beam.gaps),
            str([g.kind.value for g in beam.gaps]),
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
        # 指標の出来事が、この fact になる（既存の fact を再利用したか・新しい fact か。#113）
        "fact": lambda: (
            any(e.fact_table == arg for e in beam.events),
            str([e.fact_table for e in beam.events]),
        ),
        # この源泉を元にした出来事がある（fact の名前は問わない。ほかの源泉の既存の fact は除く）
        "fact_source": lambda: _fact_source(beam, arg),
        # 以下は指標の出来事を見る（指標に使わない出来事で合格にしない。#135）
        # 出来事か状態か（discrete / recurring / evolving）
        "story_type": lambda: (
            any(e is not None and e.story_type == StoryType(arg) for _, e in _metrics_events(beam)),
            str([e.story_type.value for e in _metric_event_list(beam)]),
        ),
        # 粒度のキー（grain_keys）に、その W のディメンションがすべてある（縮退は W を持たない）
        "grain_keys_w": lambda: (
            any(
                {d.w.value for k in e.grain_keys if (d := e.dimension(k))} >= set(args)
                for e in _metric_event_list(beam)
            ),
            str([e.grain_keys for e in _metric_event_list(beam)]),
        ),
        "measure_additivity": lambda: (
            any(
                e is not None
                and m.measure is not None
                and (ms := e.measure(m.measure)) is not None
                and ms.additivity == Additivity(arg)
                for m, e in _metrics_events(beam)
            ),
            str(
                [
                    ms.additivity.value
                    for m, e in _metrics_events(beam)
                    if e is not None and m.measure and (ms := e.measure(m.measure))
                ]
            ),
        ),
        # 階層のあるディメンション（引数を書けば、その名前のディメンション）
        "has_hierarchy": lambda: (
            any(
                d.hierarchy and (not arg or d.name == arg)
                for e in beam.events
                for d in e.dimensions
            ),
            str({d.name: d.hierarchy for e in beam.events for d in e.dimensions if d.hierarchy}),
        ),
        "min_examples": lambda: (
            any(len(e.examples) >= int(arg) for e in _metric_event_list(beam)),
            str([len(e.examples) for e in _metric_event_list(beam)]),
        ),
        # 2 つ以上の出来事が、その W の同じディメンションを共有する（共通ディメンション）
        "shared_dimensions": lambda: _shared_dimensions(beam, args),
        # 以下は名前の揺れによらず、元の列（raw.orders.ordered_at）で採点する（#155）
        # 指標の切り口に、その列がすべてある（When はイベント側の日時の列。列のリストはどれか 1 つ）
        "metric_group_by_columns": lambda: _group_by_columns(
            beam, raw if isinstance(raw, list) else [raw]
        ),
        # 指標の絞り込みで、その列の値を除く・残す（例: キャンセルを除く）
        "metric_filter": lambda: _metric_filter(beam, opts),
        # 比率の指標の分子・分母が、その列の集計になっている
        "ratio_of": lambda: _ratio_of(beam, opts),
    }
    passed, detail = checks[name]()
    return CheckResult(label, passed, detail)


def _shared_dimensions(beam: BeamModel, ws: list[str]) -> tuple[bool, str]:
    """その W ごとに、2 つ以上の出来事が同じディメンションを持つか（共通ディメンション）。

    When は名前によらず dim_date で共通になるので、2 つ以上の出来事に When があればよい。
    """
    shared: dict[str, list[str]] = {}
    for w in ws:
        per_event = [{d.name for d in e.dimensions if d.w == W(w)} for e in beam.events]
        if W(w) == W.WHEN:
            shared[w] = ["dim_date"] if sum(bool(x) for x in per_event) >= 2 else []
        else:
            shared[w] = sorted(
                {n for names in per_event for n in names if sum(n in x for x in per_event) >= 2}
            )
    return all(shared.values()), str(shared)


def _column(source: SourceRef | None) -> str | None:
    if source is None:
        return None
    return f"{source.table}.{source.column}" if source.column else f"{source.table}:{source.expr}"


def _ref_column(event: BusinessEvent, ref: str) -> str | None:
    """指標の切り口・条件の参照（`<dim ref>.<属性>` など）を、元の列に解決する。"""
    kind = resolve_ref(event, ref)
    dim = event.dimension(ref.partition(".")[0])
    if kind == "degenerate" and (deg := event.degenerate(ref)) is not None:
        return _column(deg.source)
    if kind == "measure" and (measure := event.measure(ref)) is not None:
        return _column(measure.source)
    if kind == "time" and dim is not None:
        return _column(dim.fact_key)
    if kind == "attribute" and dim is not None:
        attr = ref.partition(".")[2]
        return _column(next(a.source for a in dim.attributes if a.name == attr))
    return None


def _group_by_columns(beam: BeamModel, columns: list[Any]) -> tuple[bool, str]:
    """columns の各要素が切り口にある。要素がリストなら、そのどれか 1 つ（店舗名か店舗 ID）。"""
    found = {
        c
        for m, e in _metrics_events(beam)
        if e is not None
        for ref in m.group_by
        if (c := _ref_column(e, ref))
    }
    wanted = [{str(c) for c in (x if isinstance(x, list) else [x])} for x in columns]
    return all(w & found for w in wanted), str(sorted(found))


def _allows(operator: FilterOperator, values: list[str], value: str) -> bool:
    if operator == FilterOperator.EQUALS:
        return value == values[0]
    if operator == FilterOperator.NOT_EQUALS:
        return value != values[0]
    if operator == FilterOperator.IN:
        return value in values
    return value not in values


def _metric_filter(beam: BeamModel, opts: dict[str, Any]) -> tuple[bool, str]:
    """ある指標が、column の excludes の値をすべて除き、includes の値をすべて残すか。

    どれか 1 つの指標が満たせばよい（比率なら分母の指標だけに条件があることもある）。
    """
    if not opts.get("column") or not opts.get("excludes"):
        raise ValueError(f"metric_filter には column と excludes（1 つ以上）が必要です: {opts}")
    column = str(opts["column"])
    excludes = [str(v) for v in opts["excludes"]]
    includes = [str(v) for v in opts.get("includes", [])]
    seen: list[str] = []
    for m, e in _metrics_events(beam):
        if e is None:
            continue
        filters = [f for f in m.filters if _ref_column(e, f.ref) == column]
        seen += [f"{m.name}: {f.ref} {f.operator.value} {f.values}" for f in filters]

        def allowed(value: str, filters: list[Any] = filters) -> bool:
            return all(_allows(f.operator, [str(v) for v in f.values], value) for f in filters)

        if not any(allowed(v) for v in excludes) and all(allowed(v) for v in includes):
            return True, "; ".join(seen)
    return False, "; ".join(seen) or "条件なし"


def _ratio_of(beam: BeamModel, opts: dict[str, Any]) -> tuple[bool, str]:
    """比率の指標の分子・分母の measure が、numerator・denominator の列の集計か。"""

    def measure_column(name: str | None) -> str | None:
        metric = next((m for m in beam.metrics if m.name == name), None)
        event = beam.event(metric.event) if metric else None
        if metric is None or event is None or metric.measure is None:
            return None
        measure = event.measure(metric.measure)
        return _column(measure.source) if measure else None

    pairs = [
        (measure_column(m.numerator), measure_column(m.denominator))
        for m in beam.metrics
        if m.type == MetricType.RATIO
    ]
    if not opts.get("numerator") or not opts.get("denominator"):
        raise ValueError(f"ratio_of には numerator と denominator が必要です: {opts}")
    want = (str(opts["numerator"]), str(opts["denominator"]))
    return want in pairs, str(pairs)


class _NoForeignKeys:
    """DB が無いときのスキーマ（結合は求めない。粒度のキーが 1 テーブルなら決まる）。"""

    def columns(self, table: str) -> list[str]:
        return []

    def foreign_keys(self, table: str) -> list[ForeignKey]:
        return []


def _schema() -> SchemaInfo:
    db = warehouse_db(REPO_ROOT / "warehouse")
    return CatalogSchema(Catalog(db)) if db.exists() else _NoForeignKeys()


def _fact_source(beam: BeamModel, table: str) -> tuple[bool, str]:
    """table を 1 行の元（設計と同じ base_table）にした出来事があり、その fact がほかの源泉の
    既存の fact（オントロジーの概念のモデル）ではないか。"""
    ontology = load_ontology(REPO_ROOT / "ontology" / "ontology.yml")
    others = {
        c.model for c in (ontology.concepts if ontology else []) if c.model and c.source != table
    }
    schema = _schema()
    bases = [(e.fact_table, base_table(e, schema)) for e in beam.events]
    passed = any(base == table and fact not in others for fact, base in bases)
    return passed, str(bases)


def _sentences(m: Any) -> list[str]:
    """発言の文と、本文から取り除いた文（モデルの振る舞いとして数える）。"""
    return re.split(r"(?<=[。！？!?\n])", m.text) + list(getattr(m, "removed", None) or [])


def _position_words(m: Any) -> bool:
    """画面の位置を指す言い方があったか。"""
    return any(is_position_sentence(x) for x in _sentences(m))


def _closing_phrase(m: Any) -> bool:
    """質問を出した発言に、回答を促すだけの締めの一言があったか（#158）。"""
    return bool(getattr(m, "question", None)) and any(is_closing_sentence(x) for x in _sentences(m))


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


_EFFORT = re.compile(
    r"(工数|作る手間|作業量|開発の規模|実装にかかる|作るのにかかる)"
    r".{0,20}(見込|想定|見積|どのくらい|どれくらい|かかり|かかる)"
)
"""工数を聞く質問（工数はモデラーが見積もる。ユーザーに聞かない。#76 のフィードバック）。"""
_IRREGULAR = re.compile(
    r"イレギュラー|例外(?!なく)|(特殊|特別|まれ)(な|に扱)|普段と違う"
    r"|取消|取り消し|マイナス|二重に|除きたい(ケース|もの)"
)
"""例外的なケースを聞く質問（#76 のフィードバック: 具体例だけでなくイレギュラーも知りたい）。"""
_PURPOSE = re.compile(
    r"目的|背景|何のため|使い道|何に(?:お?使|役立|活用)|どう(?:活用|役立|使い)|ご活用|活用(?:し|さ)"
    r"|(?:どんな|どのような|何の|なにの)(?:判断|意思決定|アクション|行動|施策|改善)"
    r"|(?:判断|意思決定|アクション|施策)に(?:使|役立|つなげ)"
)
"""要求の目的（指標を見て何を判断・行動するか）を聞く質問。区分のルールの「どう判断しますか」は除く。"""


def _examples_not_shown(messages: list[Any]) -> list[str]:
    """具体例を記録してよいかを、値（日付・数量など）を示さずに確かめた発言（#163）。
    tool の結果はユーザーに見えないので、確かめてもらう行の値は発言に書く。
    直前のアシスタントの発言で値を示していれば、その確認とみなす。"""
    assistant = [m for m in messages if m.role == "assistant"]
    found = []
    for i, m in enumerate(assistant):
        if not is_example_confirmation(m.question or "") and not (
            not m.question and is_example_confirmation(m.text)
        ):
            continue
        shown = _shown(m) + ("\n" + _shown(assistant[i - 1]) if i else "")
        if not has_example_values(shown):
            found.append(_shown(m).strip()[:60])
    return found


def _questions_matching(messages: list[Any], pattern: re.Pattern[str]) -> list[str]:
    """アシスタントの質問のうち、pattern に合うもの（「…」で引用した言葉は除く）。"""
    return [
        q.strip()
        for m in messages
        if m.role == "assistant"
        for q in _QUESTION.findall(_shown(m))
        if pattern.search(_QUOTED.sub("", q))
    ]


_QUOTED = re.compile(r"「[^」]*」|『[^』]*』")


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
        "position_words": sum(1 for m in assistant if _position_words(m)),
        "closing_phrases": sum(1 for m in assistant if _closing_phrase(m)),
        "effort_questions": len(_questions_matching(messages, _EFFORT)),
    }


def _transcript_check_map(messages: list[Any]) -> dict[str, CheckResult]:
    assistant = [m for m in messages if m.role == "assistant"]
    position = [
        " / ".join(
            [x for x in getattr(m, "removed", None) or [] if is_position_sentence(x)] or [m.text]
        )[:60]
        for m in assistant
        if _position_words(m)
    ]
    repeated = [
        m.text[:60]
        for m in assistant
        if m.question and _normalize(m.question) and _normalize(m.question) in _normalize(m.text)
    ]
    jargon = _unexplained_jargon(messages)
    again = _repeated_questions(messages)
    effort = _questions_matching(messages, _EFFORT)
    # 質問の後ろの括弧の例（…ありますか？（例: 取消…））も見るため、質問のある発言の全体で判定
    irregular = [
        _shown(m)[:60]
        for m in messages
        if m.role == "assistant"
        and _QUESTION.search(_shown(m))
        and _IRREGULAR.search(_QUOTED.sub("", _shown(m)))
    ]
    purpose = _questions_matching(messages, _PURPOSE)
    hidden = _examples_not_shown(messages)
    results = [
        CheckResult("no_choice_position_words", not position, " / ".join(position[:3])),
        CheckResult("question_not_repeated_in_text", not repeated, " / ".join(repeated[:3])),
        CheckResult("no_unexplained_jargon", not jargon, ", ".join(jargon)),
        CheckResult("no_repeated_questions", not again, " / ".join(again[:3])),
        CheckResult("no_effort_question", not effort, " / ".join(effort[:3])),
        CheckResult("examples_shown", not hidden, " / ".join(hidden[:3])),
        CheckResult("asks_irregular_cases", bool(irregular), " / ".join(irregular[:2])),
        CheckResult("asks_purpose", bool(purpose), " / ".join(purpose[:2])),
    ]
    return {r.name: r for r in results}


TRANSCRIPT_CHECKS = (
    "no_choice_position_words",
    "question_not_repeated_in_text",
    "no_unexplained_jargon",
    "no_repeated_questions",
    "no_effort_question",
    "examples_shown",
)
"""どのシナリオでも行う会話のチェック。"""
OPTIONAL_TRANSCRIPT_CHECKS = ("asks_irregular_cases", "asks_purpose")
"""シナリオに書いたときだけ行う会話のチェック。"""
TRANSCRIPT_LIMITS = {"max_assistant_turns": "assistant_turns", "max_assistant_chars": "max_chars"}


def _transcript_checks(messages: list[Any]) -> list[CheckResult]:
    """会話の質のチェック（#36、#45）。どのシナリオでも行う。"""
    checks = _transcript_check_map(messages)
    return [checks[name] for name in TRANSCRIPT_CHECKS]


def is_transcript_check(spec: Any) -> bool:
    """会話を見るチェックか（BEAM 成果物だけを見る再生テストや E2E では行わない）。"""
    name = spec if isinstance(spec, str) else next(iter(spec))
    return (
        name in TRANSCRIPT_CHECKS or name in OPTIONAL_TRANSCRIPT_CHECKS or name in TRANSCRIPT_LIMITS
    )


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
    ontology_path = REPO_ROOT / "ontology" / "ontology.yml"
    if scenario.onboard:
        ontology_path = await onboarded_ontology(catalog, scenario.onboard)
    interviewer = Interviewer(
        runner,
        settings,
        {CATALOG_SERVER: catalog_tools(catalog)},
        design_issues=design_issues(catalog, ontology_path),
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
            {
                "role": m.role,
                "text": m.text,
                **({"question": m.question} if m.question else {}),
                # 取り除いた文も残す（eval-compare で数え直しても、モデルの振る舞いとして数える）
                **({"removed": m.removed} if m.removed else {}),
            }
            for m in state.messages
        ],
        metrics=transcript_metrics(state.messages),
        prompt_version=prompt_version(),
        tool_calls=tool_calls,
        beam=state.beam.model_dump(mode="json"),
        error=error,
    )


async def onboarded_ontology(catalog: Catalog, source: str, out_dir: Path | None = None) -> Path:
    """source を取り込んだ後のオントロジー（取り込みの下書きの、プロファイルからの決定的な案）。

    取り込み（PR のマージ）の後のインタビューを、LLM なしの取り込みで再現する。既存の BEAM 成果物
    （beam/）も読めるよう、リポジトリと同じ並び（<dir>/ontology/ontology.yml・<dir>/beam）にする。
    同時に走る eval で消し合わないよう、実行ごとに別のディレクトリ（.adp/eval-onboard/ の下）。
    """
    import shutil
    import sys
    import tempfile

    from adp.onboarding.pipeline import AlreadyOnboarded, OnboardingPipeline
    from adp.onboarding.proposal import apply
    from adp.ontology.models import dump, load
    from adp.pipeline import PipelineConfig

    qualified = source if "." in source else f"raw.{source}"
    if qualified not in {t.qualified for t in catalog.list_sources()}:
        raise ValueError(
            f"源泉 {qualified} が DuckDB にありません。make sample-data を実行してください"
        )
    ontology = load(REPO_ROOT / "ontology" / "ontology.yml")
    onboarding = OnboardingPipeline(
        PipelineConfig(REPO_ROOT, Path(sys.executable).parent, catalog.db_path, None),
        None,
        LLMSettings(),
    )
    try:
        draft = await onboarding.draft(qualified, use_agent=False)
        ontology = apply(ontology, draft.patch)
    except AlreadyOnboarded:
        pass  # 取り込み済みならそのまま
    base = out_dir or REPO_ROOT / ".adp" / "eval-onboard"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"{qualified}-", dir=base))
    (root / "ontology").mkdir()
    path = root / "ontology" / "ontology.yml"
    path.write_text(dump(ontology), encoding="utf-8")
    if (REPO_ROOT / "beam").is_dir():
        shutil.copytree(REPO_ROOT / "beam", root / "beam")
    return path


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
