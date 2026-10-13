"""インタビューのシステムプロンプト（#108）。"""

from __future__ import annotations

import re

from adp.interview import new_state
from adp.interview.prompts import INTERVIEW_PATH, system_prompt
from adp.interview.state import PHASE_LABELS, Phase


def _section(start: str, end: str) -> str:
    return system_prompt().split(start, 1)[1].split(end, 1)[0]


def test_review_does_not_urge_finalize_while_issues_remain() -> None:
    """課題が残っているあいだは確定を促さず、課題を解消させる（#108）。"""
    review = _section("## 7. review", "## 8. finalized")
    assert "get_beam" in review
    assert "課題が残っているあいだは" in review
    assert "確定を促さず" in review
    # 画面の実際のボタン名で伝え、確定を促すのは課題が無くなってから
    assert "「確定して実装へ」" in review
    assert review.index("課題が無くなったら") < review.index("ボタンを押すよう伝える")


def test_cannot_press_and_unresolvable_issues_apply_at_any_phase() -> None:
    """「押せない」への応じ方と、会話で解消できない課題（設計）の扱いは段階によらない（#108）。"""
    rules = _section("# 記録のルール", "# 安全")
    assert "押せない" in rules
    assert "どの段階でも" in rules
    assert "設計:" in rules
    assert "同じ質問を繰り返さず" in rules
    assert "取り込む" in rules


def test_interview_phases_match_the_code() -> None:
    """段階の正本は interview.md の見出し。コードの段階と同じ順・名前・表示名（#136）。"""
    text = INTERVIEW_PATH.read_text(encoding="utf-8")
    headings = re.findall(r"^## (\d+)\. (\w+)（(.+)）$", text, re.MULTILINE)
    assert [(int(n), name, label) for n, name, label in headings] == [
        (i + 1, p.value, PHASE_LABELS[p]) for i, p in enumerate(Phase)
    ]
    assert text in system_prompt()


def test_interview_mentions_only_existing_tools() -> None:
    """interview.md の文中の tool の名前は、インタビューの tool に実在する（#136）。"""
    from adp.catalog.tools import allowed_tool_names
    from adp.interview.tools import build_tools

    names = {t.name for t in build_tools(new_state("x"))}
    names |= {n.rsplit("__", 1)[-1] for n in allowed_tool_names()}
    text = INTERVIEW_PATH.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"`([a-z]+_[a-z_]+)`", text))
    assert mentioned and mentioned <= names, mentioned - names


def test_prompt_has_no_phase_policy_of_its_own() -> None:
    """プロンプト本体は段階の方針を持たない（interview.md の 1 か所だけ。#136）。"""
    from adp.interview.prompts import SYSTEM_PROMPT

    assert not re.search(r"^\d\. (framing|seven_ws|grain|review)", SYSTEM_PROMPT, re.MULTILINE)
