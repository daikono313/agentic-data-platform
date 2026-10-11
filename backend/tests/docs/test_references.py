"""ドキュメントに書いたファイルの場所とリンクが、リポジトリの中身と合っているか。

コードブロックの中は照合しない（コマンドの例で、パスやリンクとしては書いていないため）。
"""

import re
from pathlib import Path

from tests.docs._docs import REPO_ROOT, doc_files, doc_text, inline_code, line_of, rel
from tests.docs._docs import without_fences as body_of

# この接頭辞で始まるものは、拡張子が無くてもリポジトリのルートからのパスとして読む
REPO_PREFIXES = (
    "backend/",
    "frontend/",
    "warehouse/",
    "docs/",
    "evals/",
    "ontology/",
    "beam/",
    "scripts/",
    ".github/",
)

# 実行時にできる場所（git では追跡しない）。ここを指すパスは照合しない
RUNTIME_PREFIXES = (".adp/", "warehouse/data/", "warehouse/target/", "target/")

# 相対パスを探す起点。ドキュメントのディレクトリとリポジトリのルートに加え、docs/ の設計書が
# コンポーネントを省いて書く場所（`tests/...` は backend/、`charts/` は warehouse/ など）
COMPONENT_ROOTS = (
    REPO_ROOT / "backend",
    REPO_ROOT / "backend" / "src" / "adp",
    REPO_ROOT / "warehouse",
    REPO_ROOT / "frontend",
)

# パスに見えるが、リポジトリに無くてよいもの（理由つき）
NOT_IN_REPO = {
    "beam/<event>-<yyyymmdd>": "生成ジョブのブランチ名で、ファイルではない",
    "faces/": "charts/ が無いときに dbt Charts が配信する場所。置けないと書いている（ADR 0002）",
    "Charts/": "大文字・小文字の違いで迂回する例（guardrails）",
    "chartſ/": "ſ で迂回する例（guardrails）",
}

_PLACEHOLDER = re.compile(r"<[^>]*>|…|\{[^},]*\}")


def path_token(code: str) -> str | None:
    """インラインコードの最初の語がパスなら、その部分（行番号・見出しの指定を落とす）。

    パスとみなすのは、リポジトリの接頭辞で始まるものと、`/` を含み拡張子があるか `/` で終わるもの。
    """
    words = code.split()
    if not words or "://" in words[0] or words[0].startswith("~"):
        return None
    token = re.sub(r"(#.*|:\d+)$", "", words[0]).rstrip(".,、。)）")
    if token.startswith(REPO_PREFIXES):
        return token
    if "/" in token and (token.endswith("/") or re.search(r"\.[A-Za-z0-9*]+$", token)):
        return token
    return None


def expand_braces(path: str) -> list[str]:
    """`{a,b}` をそれぞれの候補に展開する（`{id}` のような 1 語の波括弧はプレースホルダ）。"""
    m = re.search(r"\{([^{}]*,[^{}]*)\}", path)
    if not m:
        return [path]
    return [
        alt
        for choice in m.group(1).split(",")
        for alt in expand_braces(path[: m.start()] + choice + path[m.end() :])
    ]


def _exists_from(base: Path, path: str) -> bool:
    pattern = _PLACEHOLDER.sub("*", path).rstrip("/")
    if any(c in pattern for c in "*?["):
        return any(base.glob(pattern))
    return (base / pattern).exists()


def _runtime(base: Path, path: str) -> bool:
    relative = (base / path).resolve().relative_to(REPO_ROOT.resolve()).as_posix() + "/"
    return path.startswith(RUNTIME_PREFIXES) or relative.startswith(RUNTIME_PREFIXES)


def missing_paths(doc: Path) -> list[str]:
    """ドキュメントの中で、どの起点（ドキュメントのディレクトリ・ルート・COMPONENT_ROOTS）にも無いパス。"""
    bases = [doc.parent, REPO_ROOT, *COMPONENT_ROOTS]
    out = []
    for line, code in inline_code(doc_text(doc)):
        path = path_token(code)
        if path is None or path in NOT_IN_REPO or any(_runtime(b, path) for b in bases):
            continue
        absent = [
            alt for alt in expand_braces(path) if not any(_exists_from(b, alt) for b in bases)
        ]
        if absent:
            out.append(f"{rel(doc)}:{line} `{code}` → 見つからない: {absent}")
    return out


def test_paths_in_backticks_exist() -> None:
    missing = [m for doc in doc_files() for m in missing_paths(doc)]
    assert not missing, (
        "ドキュメントに書いたパスが、ドキュメントのディレクトリ・リポジトリのルート・"
        "COMPONENT_ROOTS のどこにも無い"
        "（直すか、無くてよいなら NOT_IN_REPO へ）:\n" + "\n".join(missing)
    )


def test_not_in_repo_allowlist_is_still_used() -> None:
    text = "\n".join(doc_text(doc) for doc in doc_files())
    unused = [p for p in NOT_IN_REPO if p not in text]
    assert not unused, f"NOT_IN_REPO に、もうどのドキュメントにも無いパスがある（消す）: {unused}"


def test_relative_links_resolve() -> None:
    broken = []
    for doc in doc_files():
        body = body_of(doc_text(doc))
        for m in re.finditer(r"\]\(([^)\s]+)\)", body):
            target = m.group(1)
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            file_part = target.split("#", 1)[0]
            # `](/x)` はリポジトリのルートから（GitHub の表示と同じ）
            dest = (
                REPO_ROOT / file_part.lstrip("/")
                if file_part.startswith("/")
                else doc.parent / file_part
            )
            if not dest.exists():
                broken.append(f"{rel(doc)}:{line_of(body, m.start())} ]({target}) → {dest}")
    assert not broken, "リンク先のファイルが無い:\n" + "\n".join(broken)


def test_path_helpers() -> None:
    assert path_token("cleansing/<schema>.<table>.yml") == "cleansing/<schema>.<table>.yml"
    assert path_token("src/adp/") == "src/adp/"
    assert path_token("docs/a.md#見出し") == "docs/a.md"
    assert path_token("feat/7-beam-schema") is None  # ブランチ名
    assert path_token("https://example.com/a.md") is None
    assert path_token("~/.claude/x.jsonl") is None
    assert expand_braces("a/{b,c}.yml") == ["a/b.yml", "a/c.yml"]
    assert expand_braces("a/{id}/x.yml") == ["a/{id}/x.yml"]
    assert _exists_from(REPO_ROOT, "backend/src/<pkg>/")
    assert not _exists_from(REPO_ROOT, "backend/nope.py")
