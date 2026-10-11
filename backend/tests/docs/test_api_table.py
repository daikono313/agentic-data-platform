"""design-doc の「API（backend）」の表と、create_app のルートの照合。"""

import re
from typing import cast

from fastapi.routing import APIRoute

from adp.api.app import create_app
from adp.api.service import SessionService
from tests.docs import REPO_ROOT, section

Endpoint = tuple[str, str]


def normalize(path: str) -> str:
    """クエリを落とし、パスパラメータの名前を揃える（`{id}` と `{session_id}` を同じに扱う）。"""
    path = path.split("?", 1)[0]
    return re.sub(r"\{[^}]*\}", "{}", path)


def documented() -> list[Endpoint]:
    text = (REPO_ROOT / "docs" / "design-doc.md").read_text(encoding="utf-8")
    rows = re.findall(
        r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`",
        section(text, "API（backend）"),
        flags=re.MULTILINE,
    )
    assert rows, "design-doc の API の表を読み取れない（表の形が変わったならテストを直す）"
    return [(method, normalize(path)) for method, path in rows]


def implemented() -> set[Endpoint]:
    # ルートの登録だけを見る（service はルートの登録時には使われない）
    app = create_app(cast(SessionService, object()))
    return {
        (method, normalize(route.path))
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
        if method != "HEAD"
    }


def test_api_table_has_no_duplicates() -> None:
    rows = documented()
    dupes = sorted({r for r in rows if rows.count(r) > 1})
    assert not dupes, f"design-doc の API の表で重複している行: {dupes}"


def test_every_route_is_documented() -> None:
    missing = sorted(implemented() - set(documented()))
    assert not missing, f"design-doc の API の表に無いルート（表に行を足す）: {missing}"


def test_every_documented_route_exists() -> None:
    stale = sorted(set(documented()) - implemented())
    assert not stale, f"design-doc の API の表にあるが実装に無いルート（表から消すか直す）: {stale}"
