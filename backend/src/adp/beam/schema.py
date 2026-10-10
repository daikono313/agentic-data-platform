"""BEAM 成果物と Event Matrix の JSON Schema を出力する。frontend の型生成に使う。

uv run python -m adp.beam.schema ../frontend/src/generated/beam.schema.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema

from adp.beam.matrix import EventMatrix
from adp.beam.models import BeamModel


def json_schema() -> dict[str, Any]:
    _, schema = models_json_schema(
        [(BeamModel, "validation"), (EventMatrix, "validation")], title="BEAM"
    )
    return schema


def main() -> None:
    text = json.dumps(json_schema(), ensure_ascii=False, indent=2) + "\n"
    if len(sys.argv) > 1:
        out = Path(sys.argv[1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
