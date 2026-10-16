"""ドキュメントの照合テストが共有する読み取りの helper（ここ 1 か所にまとめる）。"""

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# 照合するドキュメント（リポジトリのルートからの glob）。生成物（docs/ontology.md）も含めてよい
DOC_GLOBS = (
    "*.md",
    "*/CLAUDE.md",
    "*/README.md",
    "docs/**/*.md",
    ".github/*.md",
    ".claude/skills/**/*.md",
)

_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```[^\n]*$", flags=re.MULTILINE | re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")


def doc_files() -> list[Path]:
    found = {p for g in DOC_GLOBS for p in REPO_ROOT.glob(g) if p.is_file()}
    return sorted(found)


@cache
def doc_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


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


def without_fences(text: str) -> str:
    """コードブロックを空行に置き換えた本文（行番号は保つ）。パスやリンクの照合は本文だけを見る。"""
    return _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


@dataclass(frozen=True)
class MakeInvocation:
    line: int
    raw: str
    targets: tuple[str, ...]
    variables: tuple[str, ...]


def _parse_make(command: str, line: int) -> MakeInvocation | None:
    command = command.split("#", 1)[0].strip()  # コードブロックの行末コメントを落とす
    words = command.split()
    if len(words) < 2 or words[0] != "make":
        return None
    targets = tuple(w for w in words[1:] if re.fullmatch(r"[a-z][a-z0-9-]*", w))
    variables = tuple(
        m.group(1) for w in words[1:] if (m := re.fullmatch(r"([A-Z][A-Z0-9_]*)=.*", w))
    )
    return MakeInvocation(line, command, targets, variables)


def make_invocations(text: str) -> list[MakeInvocation]:
    """`make X VAR=…` の呼び出し。コードブロックの `make` で始まる行と、インラインコードの両方。"""
    found: list[MakeInvocation] = []
    for fence in _FENCE.finditer(text):
        start = line_of(text, fence.start(1))
        for offset, body_line in enumerate(fence.group(1).splitlines()):
            if inv := _parse_make(body_line, start + offset):
                found.append(inv)
    body = without_fences(text)
    for m in _INLINE.finditer(body):
        if inv := _parse_make(m.group(1), line_of(body, m.start())):
            found.append(inv)
    return found


def inline_code(text: str) -> list[tuple[int, str]]:
    """コードブロックの外のインラインコード（行番号, 中身）。"""
    body = without_fences(text)
    return [(line_of(body, m.start()), m.group(1)) for m in _INLINE.finditer(body)]
