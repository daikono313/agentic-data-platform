"""取得可能なデータセット・指標の一覧（#38）。

ユーザーが要求を出す前に「今どんなデータが見られるか」を知るためのもの。4 つの区分で返す。

- metrics: Semantic Layer の指標と、使える切り口（すぐチャートで見られる）
- models: 整備済みの marts（dim / fct）
- sources: 元データ（ソースカタログ）
- beams: 確定した BEAM 成果物（どの要求から指標が作られたか）

切り口は MetricFlow に問い合わせる（`mf list dimensions`、ADR 0001 の方針で CLI を使う）。
数秒かかるので、semantic manifest が更新されるまで結果をキャッシュする。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from adp.catalog.catalog import Catalog

MF_TIMEOUT_S = 120


class Dimension(BaseModel):
    name: str
    """MetricFlow の参照名（例: store__channel、metric_time）。"""
    label: str
    description: str = ""


class MetricInfo(BaseModel):
    name: str
    label: str
    description: str = ""
    type: str
    model: str | None = None
    """元になる fct（simple の場合）。"""
    dimensions: list[Dimension] = Field(default_factory=list)
    beam: str | None = None
    """この指標を作った BEAM 成果物（beam/<name>.yml）。"""


class ColumnInfo(BaseModel):
    name: str
    description: str = ""


class ModelInfo(BaseModel):
    name: str
    kind: str
    """dim / fct"""
    description: str = ""
    columns: list[ColumnInfo] = Field(default_factory=list)


class SourceInfo(BaseModel):
    name: str
    description: str = ""
    row_count: int
    columns: list[ColumnInfo] = Field(default_factory=list)


class BeamInfo(BaseModel):
    name: str
    title: str
    request: str
    metrics: list[str]


class Datasets(BaseModel):
    metrics: list[MetricInfo]
    models: list[ModelInfo]
    sources: list[SourceInfo]
    beams: list[BeamInfo]
    errors: list[str] = Field(default_factory=list)
    """一部が取れなかった場合の理由（一覧全体は返す）。"""


class DatasetIndex:
    def __init__(self, repo_root: Path, catalog: Catalog, mf: Path) -> None:
        self.repo_root = repo_root
        self.warehouse = repo_root / "warehouse"
        self.catalog = catalog
        self.mf = mf
        self._dims_cache: dict[str, tuple[float, list[str]]] = {}

    # --- 指標 ------------------------------------------------------------------------------

    def _semantic_manifest(self) -> dict[str, Any] | None:
        path = self.warehouse / "target" / "semantic_manifest.json"
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def _dimension_labels(self, manifest: dict[str, Any]) -> dict[str, tuple[str, str]]:
        """`<entity>__<dimension>` → (表示名, 説明)。"""
        labels: dict[str, tuple[str, str]] = {}
        for sm in manifest.get("semantic_models", []):
            entities = [e["name"] for e in sm.get("entities", []) if e.get("type") == "primary"]
            entities += [e["name"] for e in sm.get("entities", []) if e.get("type") == "unique"]
            for d in sm.get("dimensions", []):
                text = d.get("description") or ""
                label = d.get("label") or text.split("（")[0] or d["name"]
                for entity in entities:
                    labels[f"{entity}__{d['name']}"] = (label, text)
        return labels

    def _dimensions_for(self, metric: str) -> list[str]:
        path = self.warehouse / "target" / "semantic_manifest.json"
        mtime = path.stat().st_mtime
        cached = self._dims_cache.get(metric)
        if cached and cached[0] == mtime:
            return cached[1]
        result = subprocess.run(
            [str(self.mf), "list", "dimensions", "--metrics", metric],
            cwd=self.warehouse,
            env=os.environ | {"DBT_PROFILES_DIR": str(self.warehouse), "NO_COLOR": "1"},
            capture_output=True,
            text=True,
            check=False,
            timeout=MF_TIMEOUT_S,
        )
        if result.returncode != 0:
            raise RuntimeError(f"mf list dimensions --metrics {metric} が失敗しました")
        names = [
            line.split("•", 1)[1].strip() for line in result.stdout.splitlines() if "•" in line
        ]
        self._dims_cache[metric] = (mtime, names)
        return names

    def metrics(self, beams: list[BeamInfo], errors: list[str]) -> list[MetricInfo]:
        manifest = self._semantic_manifest()
        if manifest is None:
            errors.append("Semantic Layer の定義がありません（make dbt-build を実行してください）")
            return []
        labels = self._dimension_labels(manifest)
        by_metric = {m: b.name for b in beams for m in b.metrics}
        model_of_measure = {
            measure["name"]: sm["name"]
            for sm in manifest.get("semantic_models", [])
            for measure in sm.get("measures", [])
        }
        result = []
        for m in manifest.get("metrics", []):
            params = m.get("type_params") or {}
            # latest spec の simple metric は measure を持たず、集計の元を直接持つ
            agg = params.get("metric_aggregation_params") or {}
            measure = params.get("measure") or {}
            model = agg.get("semantic_model") or model_of_measure.get(measure.get("name", ""))
            info = MetricInfo(
                name=m["name"],
                label=m.get("label") or m["name"],
                description=m.get("description") or "",
                type=m.get("type", ""),
                model=model,
                beam=by_metric.get(m["name"]),
            )
            try:
                info.dimensions = [
                    Dimension(
                        name=d,
                        label=labels.get(d, (_time_label(d), ""))[0],
                        description=labels.get(d, ("", ""))[1],
                    )
                    for d in self._dimensions_for(m["name"])
                ]
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                errors.append(str(e))
            result.append(info)
        return result

    # --- モデル・ソース・BEAM ------------------------------------------------------------------

    def models(self, errors: list[str]) -> list[ModelInfo]:
        path = self.warehouse / "target" / "manifest.json"
        if not path.exists():
            errors.append("dbt の manifest がありません（make dbt-build を実行してください）")
            return []
        manifest = json.loads(path.read_text(encoding="utf-8"))
        result = []
        for node in manifest.get("nodes", {}).values():
            name = node.get("name", "")
            if node.get("resource_type") != "model" or not name.startswith(("dim_", "fct_")):
                continue
            result.append(
                ModelInfo(
                    name=name,
                    kind=name.split("_", 1)[0],
                    description=(node.get("description") or "").strip(),
                    columns=[
                        ColumnInfo(name=c["name"], description=c.get("description") or "")
                        for c in node.get("columns", {}).values()
                    ],
                )
            )
        return sorted(result, key=lambda m: (m.kind != "fct", m.name))

    def sources(self, errors: list[str]) -> list[SourceInfo]:
        try:
            tables = self.catalog.list_sources()
            return [
                SourceInfo(
                    name=t.qualified,
                    description=t.description,
                    row_count=t.row_count,
                    columns=[
                        ColumnInfo(name=c.name, description=c.description)
                        for c in self.catalog.describe_table(t.qualified).columns
                    ],
                )
                for t in tables
            ]
        except Exception as e:  # DB が使用中など。一覧の他の部分は返す
            errors.append(f"元データの一覧を取得できませんでした: {type(e).__name__}")
            return []

    def beams(self, errors: list[str]) -> list[BeamInfo]:
        result = []
        for path in sorted((self.repo_root / "beam").glob("*.yml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                result.append(
                    BeamInfo(
                        name=data["name"],
                        title=data.get("title", data["name"]),
                        request=data.get("request", ""),
                        metrics=[m["name"] for m in data.get("metrics", [])],
                    )
                )
            except Exception:
                errors.append(f"{path.name} を読めませんでした")
        return result

    def all(self) -> Datasets:
        errors: list[str] = []
        beams = self.beams(errors)
        return Datasets(
            metrics=self.metrics(beams, errors),
            models=self.models(errors),
            sources=self.sources(errors),
            beams=beams,
            errors=errors,
        )


def _time_label(name: str) -> str:
    if name == "metric_time":
        return "日付（指標の時間軸）"
    return name
