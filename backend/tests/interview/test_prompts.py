"""インタビューのシステムプロンプト（#108）。"""

from __future__ import annotations

from adp.interview.prompts import system_prompt


def _section(start: str, end: str) -> str:
    return system_prompt().split(start, 1)[1].split(end, 1)[0]


def test_review_does_not_urge_finalize_while_issues_remain() -> None:
    """課題が残っているあいだは確定を促さず、課題を解消させる（#108）。"""
    review = _section("7. review:", "# 記録のルール")
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
