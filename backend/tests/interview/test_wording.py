"""画面の位置を指す文（#77）と、質問を出したときの締めの一言（#158）を本文から取り除く。"""

import pytest

from adp.interview.wording import clean_reply, has_example_values, is_example_confirmation


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
    assert clean_reply(text) == (kept, removed)


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
    assert clean_reply(text) == (text, [])


@pytest.mark.parametrize(
    ("text", "kept", "removed"),
    [
        (
            "店舗は 20 か所です。\n\n回答をお待ちしています。",
            "店舗は 20 か所です。",
            ["回答をお待ちしています。"],
        ),
        (
            "「なぜ」の切り口について、お返事をお待ちしています。",
            "",
            ["「なぜ」の切り口について、お返事をお待ちしています。"],
        ),
        ("ユーザーの回答待ちです。", "", ["ユーザーの回答待ちです。"]),
        ("この例でよいか、お選びください。", "", ["この例でよいか、お選びください。"]),
        (
            "ほかに見たい切り口があるかを、ボタンでお答えください。",
            "",
            ["ほかに見たい切り口があるかを、ボタンでお答えください。"],
        ),
        ("お選びください。", "", ["お選びください。"]),
        ("質問と選択肢を表示しました。", "", ["質問と選択肢を表示しました。"]),
        (
            "目的を伺います。近いものを選ぶか、自由に入力してください（複数選択も可です）。",
            "目的を伺います。",
            ["近いものを選ぶか、自由に入力してください（複数選択も可です）。"],
        ),
        (
            "自由に書いていただいても構いません。",
            "",
            ["自由に書いていただいても構いません。"],
        ),
    ],
)
def test_strips_closing_sentences_with_question(text: str, kept: str, removed: list[str]) -> None:
    assert clean_reply(text, question=True) == (kept, removed)
    # 質問が無ければ締めの一言は残す
    assert clean_reply(text) == (text, [])


@pytest.mark.parametrize(
    "text",
    [
        "特に無ければ「今回は不要」を選んでください。",  # 具体的な答え方
        "データに無さそうなものでも構いません。",
        "ほかに見たい切り口があれば教えてください。",
        "この 2 件を具体例として記録してよいか、回答をお待ちしています。",  # 数字を含む
        "2024-12-23 に出荷、4日ご回答をお待ちしています。",  # 前の行の中身とつながった
        "- 店舗名（20 店舗）\n- 回答をお待ちしています。",  # リストの行
        "」という問いで合っているか、確認をお待ちしています。",  # 「」の中で切れた後ろ半分
        "在庫の確認を待ってから出荷する運用です。",
        # 業務の説明（#160 レビュー）
        "出荷は倉庫の確認待ちです。",
        "返品は、顧客からの返事を待っています。",
        "承認されるまで、注文は確認を待ちます。",
        "顧客からの回答を待っています。",
        # 本文の別の依頼（#160 レビュー）
        "あわせて、集計に使う日付の列名も書いてください。",
        "なお、担当部署の名前もご記入ください。",
        "違う場合は正しい定義を書いてください。",
    ],
)
def test_keeps_sentences_that_are_not_just_closing(text: str) -> None:
    assert clean_reply(text, question=True) == (text, [])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("この 2 件を具体例として記録してよいですか？", True),
        ("この2件を具体例にしてよいですか？", True),
        ("この 2 件を例として残してよいですか？", True),
        ("この2件の例で合っていますか？", True),
        # 具体例の確認ではない（#166 のレビュー）
        ("キャンセルは例外として除外でよいですか？", False),
        ("例えば月単位の集計でよいですか？", False),
        ("事例として使ってよいですか？", False),
    ],
)
def test_is_example_confirmation(text: str, expected: bool) -> None:
    assert is_example_confirmation(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2024-11-24 に福岡店で", True),
        ("11 月 24 日の注文", True),
        ("07:45 に入荷", True),
        ("返品率 12.5%", True),
        ("3 個、17,100 円", True),
        ("注文ID 12", True),
        ("この 2 件", False),  # 件数は値ではない
    ],
)
def test_has_example_values(text: str, expected: bool) -> None:
    assert has_example_values(text) is expected
