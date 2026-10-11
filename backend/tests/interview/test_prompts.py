"""インタビューのシステムプロンプト（#108）。"""

from __future__ import annotations

from adp.interview.prompts import system_prompt


def test_review_does_not_urge_finalize_while_issues_remain() -> None:
    """課題が残っているあいだは確定を促さず、課題を解消する質問をさせる（#108）。"""
    review = system_prompt().split("7. review:", 1)[1].split("# 記録のルール", 1)[0]
    assert "get_beam" in review
    assert "課題が残っているあいだは" in review
    assert "確定を促したり" in review
    assert "解消する質問" in review
    # 確定を促すのは、課題が無くなってから
    assert review.index("課題が無くなったら") < review.index("「確定」ボタンを押すよう伝える")
