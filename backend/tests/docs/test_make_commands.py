"""ドキュメントに書いた `make` の呼び出しと、Makefile のターゲット・変数の照合。"""

import re

from tests.docs._docs import REPO_ROOT, doc_files, doc_text, make_invocations, rel, section

# CLAUDE.md の「コマンド」に書かなくてよい、内部用のターゲット（他のターゲットから呼ばれる部品）
INTERNAL_TARGETS = {
    "sample-data",  # make setup が呼ぶ（SAMPLE_ORDERS で件数を変えるときに単独でも使う）
    "dev-backend",  # make dev が呼ぶ
    "dev-frontend",  # make dev が呼ぶ
    "check-secrets",  # make lint が呼ぶ
}


def recipes() -> dict[str, str]:
    """ターゲット名 → レシピ（次のターゲットまでの行）。"""
    out: dict[str, str] = {}
    current: str | None = None
    for line in doc_text(REPO_ROOT / "Makefile").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_-]*)\s*:(?!=)", line)
        if m:
            current = m.group(1)
            out[current] = ""
        elif current is not None and line.startswith("\t"):
            out[current] += line + "\n"
    return out


def test_makefile_parser_sees_phony_targets() -> None:
    # パーサの確認: .PHONY に書いたターゲットはすべて定義として読めている
    m = re.search(r"^\.PHONY:(.*)$", doc_text(REPO_ROOT / "Makefile"), re.MULTILINE)
    assert m, "Makefile に .PHONY が無い"
    unparsed = set(m.group(1).split()) - set(recipes())
    assert not unparsed, f"Makefile の .PHONY にあるが定義として読めないターゲット: {unparsed}"


def test_claude_md_lists_every_public_target() -> None:
    text = doc_text(REPO_ROOT / "CLAUDE.md")
    documented = {t for inv in make_invocations(section(text, "コマンド")) for t in inv.targets}
    assert documented, "CLAUDE.md の「コマンド」から make の行を読み取れない"
    public = set(recipes()) - INTERNAL_TARGETS
    assert not (public - documented), (
        "CLAUDE.md の「コマンド」に無いターゲット（足すか、内部用なら INTERNAL_TARGETS へ）: "
        f"{sorted(public - documented)}"
    )
    assert not (documented & INTERNAL_TARGETS), (
        f"内部用（INTERNAL_TARGETS）のターゲットが CLAUDE.md にある: "
        f"{sorted(documented & INTERNAL_TARGETS)}（公開するなら INTERNAL_TARGETS から外す）"
    )


def test_make_invocations_in_docs_are_valid() -> None:
    """すべてのドキュメントの `make X VAR=…` について、X がターゲットにあり、VAR をレシピが読む。"""
    all_recipes = recipes()
    errors: list[str] = []
    for path in doc_files():
        for inv in make_invocations(doc_text(path)):
            where = f"{rel(path)}:{inv.line} `{inv.raw}`"
            if not inv.targets:
                errors.append(f"{where}: ターゲットを読み取れない")
            unknown = [t for t in inv.targets if t not in all_recipes]
            if unknown:
                errors.append(f"{where}: Makefile に無いターゲット {unknown}")
            recipe = "".join(all_recipes.get(t, "") for t in inv.targets)
            unused = [v for v in inv.variables if f"$({v})" not in recipe]
            if unused:
                errors.append(f"{where}: {list(inv.targets)} のレシピが読まない変数 {unused}")
    assert not errors, "ドキュメントの make の呼び出しが Makefile と合わない:\n" + "\n".join(errors)


def test_make_invocation_parser() -> None:
    text = (
        "```\nmake lint test  # 両方 REPEAT=1\nmake e2e SCENARIO=x V2=1\n```\n"
        "本文の `make eval REPEAT=3` も読む\n"
    )
    got = [(inv.line, inv.targets, inv.variables) for inv in make_invocations(text)]
    assert got == [
        (2, ("lint", "test"), ()),
        (3, ("e2e",), ("SCENARIO", "V2")),
        (5, ("eval",), ("REPEAT",)),
    ]
