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
            "優先度を決めます。\n優先度を、上の選択肢から選んでください。",
            "優先度を決めます。",
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
        "売上の項目は orders.amount です。",
        "売上の確認をさせてください。",
        "以上の確認事項で進めます。",
        "以下の項目で確定してよいですか。",
        "上記の候補は orders テーブルにあります。",
        "前記の質問で伺った通り、期間は月次です。",
        "確定ボタンから送信されます。",
        "選択肢から選ぶ必要はありません、自由に書いてください。",
        "**以下の候補**があります。\n1. 売上\n2. 粗利",
        "| amount | 上の選択肢から選んでください |",
        "```sql\nselect amount -- 上の選択肢から選んでください\n```",
        "上の選択肢から選んでください。",  # 全部消えるなら元のまま残す
    ],
)
def test_keeps_other_sentences(text: str) -> None:
    assert strip_position_sentences(text) == (text, [])
