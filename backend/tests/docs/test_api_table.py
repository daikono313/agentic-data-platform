"""design-doc の「API（backend）」の表と、create_app のルートの照合。"""

import re
from typing import cast

from fastapi.routing import APIRoute

from adp.api.app import create_app
from adp.api.service import SessionService
from tests.docs._docs import REPO_ROOT, doc_text, line_of

DESIGN_DOC = REPO_ROOT / "docs" / "design-doc.md"
WHERE = "docs/design-doc.md の「API（backend）」の表"

Endpoint = tuple[str, str]


def normalize(path: str) -> str:
    """クエリを落とし、パスパラメータの名前を揃える（`{id}` と `{session_id}` を同じに扱う）。"""
    path = path.split("?", 1)[0]
    return re.sub(r"\{[^}]*\}", "{}", path)


def documented() -> list[tuple[Endpoint, int]]:
    """表の行（エンドポイント, design-doc の行番号）。"""
    text = doc_text(DESIGN_DOC)
    head = re.search(r"^#+\s+API（backend）\s*$", text, flags=re.MULTILINE)
    assert head, f"{WHERE} の見出しが見つからない"
    end = re.search(r"^#+\s", text[head.end() :], flags=re.MULTILINE)
    stop = head.end() + end.start() if end else len(text)
    row = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`", flags=re.MULTILINE)
    rows = [
        ((m.group(1), normalize(m.group(2))), line_of(text, m.start()))
        for m in row.finditer(text, head.end(), stop)
    ]
    assert rows, f"{WHERE} を読み取れない（表の形が変わったならテストを直す）"
    return rows


def implemented() -> set[Endpoint]:
    # いまはルートがすべて無条件に登録され、service はルートの登録時には使われない。
    # 依存（datasets・charts など）の有無でルートを出し分けるようになったら、
    # ここでダミーの依存を create_app に渡して、すべてのルートが登録されるようにする。
    app = create_app(cast(SessionService, object()))
    return {
        (method, normalize(route.path))
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
        if method != "HEAD"
    }


def test_api_table_has_no_duplicates() -> None:
    seen: dict[Endpoint, int] = {}
    dupes = []
    for ep, ln in documented():
        if ep in seen:
            dupes.append(f"{ep[0]} {ep[1]}（{seen[ep]} 行目と {ln} 行目）")
        seen.setdefault(ep, ln)
    assert not dupes, f"{WHERE} で重複している行: {dupes}"


def test_every_route_is_documented() -> None:
    missing = sorted(implemented() - {ep for ep, _ in documented()})
    assert not missing, (
        f"{WHERE} に無いルート（行を足す）: {[f'{m} {p}' for m, p in missing]}。"
        f"実装: adp.api.app.create_app"
    )


def test_every_documented_route_exists() -> None:
    actual = implemented()
    stale = [f"{ln} 行目: {m} {p}" for (m, p), ln in documented() if (m, p) not in actual]
    assert not stale, (
        f"{WHERE} にあるが create_app に無いルート（表から消すか直す）: {stale}。"
        f"実装にあるルート: {sorted(f'{m} {p}' for m, p in actual)}"
    )
