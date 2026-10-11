"""ドキュメントに書いたファイルの場所・リンク・シナリオの数が、リポジトリの中身と合っているか。"""

import re
from pathlib import Path

from tests.docs import REPO_ROOT, doc_files, rel

# リポジトリのルートからのパスとして読む、バッククォートの中の書き出し
PATH_PREFIXES = (
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

# パスに見えるが、リポジトリに無くてよいもの（理由つき）
NOT_IN_REPO = {
    "warehouse/data/*.duckdb": "DuckDB のファイル。make setup などが作り、git では追跡しない",
    "beam/<event>-<yyyymmdd>": "生成ジョブのブランチ名で、ファイルではない",
}


def _path_like(code: str) -> str | None:
    words = code.split()
    if not words or not words[0].startswith(PATH_PREFIXES):
        return None
    # `backend/src/adp/x.py:12` や `docs/a.md#見出し` の後ろを落とす
    path = re.sub(r"(#.*|:\d+)$", "", words[0]).rstrip(".,、。)）")
    return path


def _exists(path: str) -> bool:
    if any(c in path for c in "<*…{["):
        # `<name>`・`…`・`{a,b}` は任意の名前として glob で探す
        pattern = re.sub(r"<[^>]*>|…|\{[^}]*\}|\[[^\]]*\]", "*", path)
        return any(REPO_ROOT.glob(pattern.rstrip("/") or "."))
    return (REPO_ROOT / path).exists()


def test_paths_in_backticks_exist() -> None:
    missing = sorted(
        f"{rel(doc)}: {path}"
        for doc in doc_files()
        for code in re.findall(r"`([^`\n]+)`", doc.read_text(encoding="utf-8"))
        if (path := _path_like(code)) is not None and path not in NOT_IN_REPO and not _exists(path)
    )
    assert not missing, f"書いたパスがリポジトリに無い（直すか NOT_IN_REPO へ）: {missing}"


def test_not_in_repo_allowlist_is_still_used() -> None:
    text = "\n".join(doc.read_text(encoding="utf-8") for doc in doc_files())
    unused = [p for p in NOT_IN_REPO if p not in text]
    assert not unused, f"NOT_IN_REPO に、もうどのドキュメントにも無いパスがある: {unused}"


def test_relative_links_resolve() -> None:
    broken = []
    for doc in doc_files():
        for target in re.findall(r"\]\(([^)\s]+)\)", doc.read_text(encoding="utf-8")):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            if not (doc.parent / target.split("#", 1)[0]).exists():
                broken.append(f"{rel(doc)}: {target}")
    assert not broken, f"リンク先のファイルが無い: {broken}"


def _count(doc: Path, pattern: str) -> int:
    m = re.search(pattern, doc.read_text(encoding="utf-8"))
    assert m, f"{rel(doc)} に「{pattern}」が見つからない（文言を変えたならこのテストも直す）"
    return int(m.group(1))


def test_scenario_counts() -> None:
    interview = len(list((REPO_ROOT / "evals" / "interview").glob("*.yml")))
    recorded = len(list((REPO_ROOT / "backend" / "tests" / "fixtures" / "evals").glob("*.json")))
    evals_readme = REPO_ROOT / "evals" / "README.md"
    assert _count(evals_readme, r"いまは (\d+) シナリオ") == interview
    assert _count(evals_readme, r"fixtures/evals/`。(\d+) シナリオ分") == recorded
    assert _count(REPO_ROOT / "README.md", r"実際の LLM、(\d+) シナリオ") == interview
