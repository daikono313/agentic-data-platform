"""画面の位置を指す文（#77）と、質問を出したときの締めの一言（#158）を本文から取り除く。"""

import pytest

from adp.interview.wording import (
    clean_reply,
    has_example_values,
    hidden_confirmation,
    is_example_confirmation,
    join_chunks,
)


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
        # 確かめている最中だと言うだけの文（#170）
        (
            "言い換えで合っているかを、選択肢で確認しています。",
            "",
            ["言い換えで合っているかを、選択肢で確認しています。"],
        ),
        ("この理解で合っているか、確認中です。", "", ["この理解で合っているか、確認中です。"]),
        ("確認をお待ちしています。", "", ["確認をお待ちしています。"]),
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
    assert clean_reply(text, question="質問？") == (kept, removed)
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
        # 業務の確認（#174 レビュー）
        "店舗では毎月、帳簿と実在庫が合っているかを確認しています。",
        "出荷前に、数量が伝票と合っているか確認しています。",
        "経理で金額が合っているかを確認中です。",
        "現在、取引先の確認をお待ちしています。",
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
    assert clean_reply(text, question="質問？") == (text, [])


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


@pytest.mark.parametrize(
    ("question", "visible", "expected"),
    [
        ("この言い換えで合っていますか？", "", "reframe"),
        ("この言い換えで合っていますか？", "回答をお待ちしています。", "reframe"),
        (
            "この言い換えで合っていますか？",
            "「月ごとの売上金額はいくらか？」と言い換えました。",
            None,
        ),
        ("「店舗別の入荷数量はいくつか」という言い換えでよいですか？", "", None),
        (
            "この言い換え（カテゴリ別の返品数量 ÷ 販売数量の割合はいくらか）で合っていますか？",
            "",
            None,
        ),
        ("この 2 件を具体例として記録してよいですか？", "", "examples"),
        ("この 2 件を具体例として記録してよいですか？", "2024-11-24 に 3 個", None),
        ("支払方法のほかに見たい切り口はありますか？", "", None),
    ],
)
def test_hidden_confirmation(question: str, visible: str, expected: str | None) -> None:
    assert hidden_confirmation(question, visible) == expected


def test_hidden_reframe_ignores_user_text_and_follow_up_questions() -> None:
    """言い換えた問いは、アシスタントが確認より前に書いたものだけを見る（#167 のレビュー）。"""
    confirm = "この言い換えで合っていますか？"
    # ユーザーの要求文の「どれだけ」「何日」では通さない
    assert hidden_confirmation(confirm, "", "在庫がどれだけ増えたかを見たい") == "reframe"
    # 確認の後ろの別の質問では通さない
    assert hidden_confirmation(confirm + "返品率の分母は何にしますか？", "") == "reframe"
    # 具体例は、ユーザーが自分で書いた値でもよい
    assert hidden_confirmation("この例で合っていますか？", "", "2024-05-01 に 3 個") is None


@pytest.mark.parametrize(
    "visible",
    [
        "「商品カテゴリ別の返品数 ÷ 販売数」と言い換えました。",
        "「月別・店舗別の購入顧客数はいくつか」と言い換えました。",
        "この言い換え（商品カテゴリ別に、返品された個数 ÷ 販売した個数）",
    ],
)
def test_reframe_shown_variants(visible: str) -> None:
    assert hidden_confirmation("この言い換えで合っていますか？", visible) is None


def test_reframe_confirmation_across_sentences() -> None:
    """「言い換えました。合っていますか？」も言い換えの確認として見る。"""
    assert hidden_confirmation("言い換えました。合っていますか？", "") == "reframe"


def test_does_not_split_inside_quotes() -> None:
    """「」の中の疑問符で文を切らない。問いを引用した文は、位置の言葉があっても残す（#170）。"""
    text = (
        "「店舗ごとの売上金額の合計はいくらか？」という言い換えで合っているか、"
        "上の選択肢からお答えください。"
    )
    assert clean_reply(text, question="質問？") == (text, [])
    # 引用した問いが質問文にあれば、ユーザーに見えるので取り除く
    reframe = "「店舗ごとの売上金額の合計はいくらか」という言い換えで合っていますか？"
    assert clean_reply(text, question=reframe) == ("", [text])
    out, removed = clean_reply(
        "「なぜ？」の切り口です。上の選択肢から選んでください。", question="質問？"
    )
    assert out == "「なぜ？」の切り口です。"
    assert removed == ["上の選択肢から選んでください。"]
    # 閉じない「は普通の文字として扱う
    assert clean_reply("「途中の文。上の選択肢から選んでください。") == (
        "「途中の文。",
        ["上の選択肢から選んでください。"],
    )


def test_quoted_question_matches_question_loosely() -> None:
    """引用した問いと質問文は、空白・読点・かぎ括弧の違いを無視して比べる（#174 レビュー）。"""
    text = "「月ごとの 売上はいくらか」という言い換えで合っているか、上の選択肢からお答えください。"
    question = "『月ごとの売上はいくらか』で合っていますか？"
    assert clean_reply(text, question=question) == ("", [text])


_REVIEWS = (
    "失礼しました。言い換えた問いは次のとおりです。\n\n**「平均点（1〜5 点）はいくつか」**",
    "言い換えた問いは次のとおりです。\n\n**「レビューの平均点はいくつか」**\n\n合っていますか？",
)
_STORES = (
    "店舗は、店舗名・地方で記録します。\n\n次は「何を」（商品）です。\n- 商品名\n- ブランド",
    "店舗は、店舗名・地方で記録しました。\n\n次は「何を」（商品）です。商品名、ブランドがあります。",
)
_EVENT = "イベントの整理:\n- 粒度: 1 注文明細 1 行\n- 単位: 個"
_RETURNS = "返品のデータは `raw.returns` にあります（3,699 件）。"


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        # tool 呼び出しをまたいだかたまりは空行で区切る（#161）
        (["- 数量: 54 個", "次は店舗です。"], "- 数量: 54 個\n\n次は店舗です。"),
        (["", "  前置きだけ。  ", ""], "前置きだけ。"),
        # 書き直したかたまり（書き出しの文が前のかたまりにある）は、後のものだけを残す。
        # 実際の記録から（reviews・inventory_balance。語尾や括弧が変わる）
        (list(_REVIEWS), _REVIEWS[1]),
        (list(_STORES), _STORES[1]),
        (
            [_RETURNS, _RETURNS.replace("（", "(").replace("）", ")") + "どう使いますか？"],
            _RETURNS.replace("（", "(").replace("）", ")") + "どう使いますか？",
        ),
        (["検索します。", _RETURNS, _RETURNS], "検索します。\n\n" + _RETURNS),
        (["条件を記録しました。", "条件を記録しました。"], "条件を記録しました。"),
        # 構造の行（リスト・番号・見出し・表・コード）と、短い文・違う書き出しでは捨てない
        (["注文" + _EVENT, "返品" + _EVENT], "注文" + _EVENT + "\n\n返品" + _EVENT),
        (
            ["1. 店舗ごとに集計します", "1. 店舗ごとに集計します"],
            "1. 店舗ごとに集計します\n\n1. 店舗ごとに集計します",
        ),
        (
            ["## 確認したい点です", "## 確認したい点です"],
            "## 確認したい点です\n\n## 確認したい点です",
        ),
        (
            ["```\n記録しました。\n```", "記録しました。"],
            "```\n記録しました。\n```\n\n記録しました。",
        ),
        (["はい。", "はい。"], "はい。\n\nはい。"),
    ],
)
def test_join_chunks(chunks: list[str], expected: str) -> None:
    assert join_chunks(chunks) == expected
