"""BEAM 成果物の YAML 入出力（beam/<name>.yml）。"""

from __future__ import annotations

from pathlib import Path

import yaml

from adp.beam.models import BeamModel


def to_yaml(model: BeamModel) -> str:
    data = model.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


def from_yaml(text: str) -> BeamModel:
    return BeamModel.model_validate(yaml.safe_load(text))


def save(model: BeamModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_yaml(model), encoding="utf-8")


def load(path: Path) -> BeamModel:
    return from_yaml(path.read_text(encoding="utf-8"))
