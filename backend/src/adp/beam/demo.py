"""フロントエンドのデモ表示用に、fixture の BEAM 成果物と Event Matrix を JSON で出力する。

uv run python -m adp.beam.demo ../frontend/src/demo/sales_by_store.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from adp.beam import build_event_matrix, load

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "beam" / "sales_by_store.yml"


def main() -> None:
    beam = load(FIXTURE)
    data = {
        "beam": beam.model_dump(mode="json"),
        "matrix": build_event_matrix(beam).model_dump(mode="json"),
        "issues": beam.finalize_issues(),
    }
    Path(sys.argv[1]).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
