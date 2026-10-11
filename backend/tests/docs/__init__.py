"""ドキュメントの事実の記述（API の表・コマンド・ファイルの場所・数）とコードの照合（#112）。

ドキュメントは人間が持ち主（CLAUDE.md の「人間と AI の担当」）。
ここで落ちたら、事実の記述を直す（T1）か、コードの側を直す。
方針の中身を変える必要があるなら、オーナーの承認を得る（certificate の T3）。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# 照合するドキュメント（リポジトリのルートからの glob）。生成物（docs/ontology.md）も含めてよい
DOC_GLOBS = (
    "*.md",
    "*/CLAUDE.md",
    "*/README.md",
    "docs/**/*.md",
    ".github/*.md",
)


def doc_files() -> list[Path]:
    found = {p for g in DOC_GLOBS for p in REPO_ROOT.glob(g) if p.is_file()}
    return sorted(found)


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def section(text: str, heading: str) -> str:
    """見出し `heading`（# の数は問わない）から、同じか上の階層の次の見出しまでの本文。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s+(.*)$", line)
        if m and m.group(2).strip() == heading:
            level = len(m.group(1))
            body: list[str] = []
            for nxt in lines[i + 1 :]:
                h = re.match(r"^(#+)\s", nxt)
                if h and len(h.group(1)) <= level:
                    break
                body.append(nxt)
            return "\n".join(body)
    raise AssertionError(f"見出し「{heading}」が見つからない")


def code_blocks(text: str) -> list[str]:
    return re.findall(r"^```[^\n]*\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)
