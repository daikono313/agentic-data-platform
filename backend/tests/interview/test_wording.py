"""画面の位置を指す文を本文から取り除く（#77）。"""

import pytest

from adp.interview.wording import strip_position_sentences


@pytest.mark.parametrize(
    ("text", "kept", "removed"),
    [
        (
            "注文のステータスは 3 種類です。\n\n上の質問にお答えください。",
            "注文のステータスは 3 種類です。",
            ["上の質問にお答えください。"],
        ),
        (
            "優先度を、上の選択肢から選んでください。",
            "",
            ["優先度を、上の選択肢から選んでください。"],
        ),
        (
            "九州は 458 人です。お手数ですが、選択肢からお選びいただくか、ご自由にご回答ください。"
            "続けます。",
            "九州は 458 人です。続けます。",
            ["お手数ですが、選択肢からお選びいただくか、ご自由にご回答ください。"],
        ),
    ],
)
def test_strips_position_sentences(text: str, kept: str, removed: list[str]) -> None:
    assert strip_position_sentences(text) == (kept, removed)


@pytest.mark.parametrize(
    "text",
    [
        "問題なければ、画面の「確定」ボタンを押してください。",
        "確定の操作は、画面の「確定」ボタンからお願いします。",
        "以下の 3 つの観点で整理しました。\n- 店舗\n- 月",
        "売上が上がった店舗を見たいのですね。",
    ],
)
def test_keeps_other_sentences(text: str) -> None:
    assert strip_position_sentences(text) == (text, [])
