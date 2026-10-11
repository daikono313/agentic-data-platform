"""CLAUDE.md・README などに書いた `make` のコマンドと、Makefile のターゲットの照合。"""

import re

from tests.docs import REPO_ROOT, code_blocks, doc_files, rel, section

# CLAUDE.md の「コマンド」に書かなくてよい、内部用のターゲット（他のターゲットから呼ばれる部品）
INTERNAL_TARGETS = {
    "sample-data",  # make setup が呼ぶ（SAMPLE_ORDERS で件数を変えるときに単独でも使う）
    "dev-backend",  # make dev が呼ぶ
    "dev-frontend",  # make dev が呼ぶ
    "check-secrets",  # make lint が呼ぶ
}


def makefile() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def recipes() -> dict[str, str]:
    """ターゲット名 → レシピ（次のターゲットまでの行）。"""
    out: dict[str, str] = {}
    current: str | None = None
    for line in makefile().splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_-]*)\s*:(?!=)", line)
        if m and m.group(1) != "PHONY":
            current = m.group(1)
            out[current] = ""
        elif current is not None and line.startswith("\t"):
            out[current] += line + "\n"
    return out


def claude_md_commands() -> list[str]:
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    blocks = code_blocks(section(text, "コマンド"))
    assert blocks, "CLAUDE.md の「コマンド」にコードブロックが無い"
    return [line for b in blocks for line in b.splitlines() if line.startswith("make ")]


def test_makefile_parser_sees_phony_targets() -> None:
    # パーサの確認: .PHONY に書いたターゲットはすべて定義として読めている
    m = re.search(r"^\.PHONY:(.*)$", makefile(), re.MULTILINE)
    assert m, "Makefile に .PHONY が無い"
    phony = set(m.group(1).split())
    assert phony <= set(recipes())


def test_claude_md_lists_every_public_target() -> None:
    documented = {line.split()[1] for line in claude_md_commands()}
    public = set(recipes()) - INTERNAL_TARGETS
    assert not (public - documented), (
        f"CLAUDE.md の「コマンド」に無いターゲット（足すか、内部用なら INTERNAL_TARGETS へ）: "
        f"{sorted(public - documented)}"
    )
    assert not (documented & INTERNAL_TARGETS), "内部用のターゲットが CLAUDE.md に書かれている"


def test_make_commands_in_docs_exist() -> None:
    targets = set(recipes())
    stale = sorted(
        f"{rel(path)}: make {target}"
        for path in doc_files()
        for target in re.findall(r"\bmake ([a-z][a-z0-9-]*)", path.read_text(encoding="utf-8"))
        if target not in targets
    )
    assert not stale, f"Makefile に無いターゲットを書いている: {stale}"


def test_documented_variables_are_used_by_the_target() -> None:
    """`make e2e V2=1` や「REPEAT=N で」と書いた変数を、そのターゲットのレシピが読んでいる。"""
    lines = list(claude_md_commands())
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    lines += [ln for b in code_blocks(readme) for ln in b.splitlines() if ln.startswith("make ")]
    all_recipes = recipes()
    unused: list[str] = []
    for line in lines:
        words = line.split()
        # `make lint test` のように複数のターゲットを並べた行もある
        line_targets = [w for w in words[1:] if w in all_recipes]
        recipe = "".join(all_recipes[t] for t in line_targets)
        for var in re.findall(r"\b([A-Z][A-Z0-9_]*)=", line):
            if f"$({var})" not in recipe:
                unused.append(f"{line.strip()} → {var}")
    assert not unused, f"ターゲットが読んでいない変数を書いている: {unused}"
