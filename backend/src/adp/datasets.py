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
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from adp.catalog.catalog import Catalog, CatalogUnavailable

MF_TIMEOUT_S = 120
MF_PARALLEL = 4


class Dimension(BaseModel):
    name: str
    """MetricFlow の参照名（例: store__channel、metric_time）。"""
    label: str
    description: str = ""
    kind: str = "categorical"
    """time / categorical"""
    entity: str | None = None
    """切り口が属する entity（例: store）。UI でまとめて表示するため。"""


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
    data_type: str = ""


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
        self._lock = threading.Lock()
        """同時に来た要求で mf を二重に起動しない。"""

    # --- 指標 ------------------------------------------------------------------------------

    def _semantic_manifest(self) -> dict[str, Any] | None:
        path = self.warehouse / "target" / "semantic_manifest.json"
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # 無い、または dbt parse が書き換え中
            return None
        return data

    def _dimension_labels(self, manifest: dict[str, Any]) -> dict[str, tuple[str, str]]:
        """`<entity>__<dimension>` → (表示名, 説明)。"""
        labels: dict[str, tuple[str, str]] = {}
        for sm in manifest.get("semantic_models", []):
            entities = [e["name"] for e in sm.get("entities", []) if e.get("type") == "primary"]
            entities += [e["name"] for e in sm.get("entities", []) if e.get("type") == "unique"]
            if sm.get("primary_entity"):  # モデル単位の主 entity（複合粒度の fct など）
                entities.append(sm["primary_entity"])
            for d in sm.get("dimensions", []):
                text = d.get("description") or ""
                label = d.get("label") or text.split("（")[0] or d["name"]
                for entity in entities:
                    labels[f"{entity}__{d['name']}"] = (label, text)
        return labels

    def _dimensions_for(self, metric: str, mtime: float) -> list[str]:
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
        time_dims = {
            f"{e}__{d['name']}"
            for sm in manifest.get("semantic_models", [])
            for d in sm.get("dimensions", [])
            if d.get("type") == "time"
            for e in [x["name"] for x in sm.get("entities", [])] + [sm.get("primary_entity") or ""]
        }
        by_name = {m["name"]: m for m in manifest.get("metrics", [])}
        by_metric = {m: b.name for b in beams for m in b.metrics}
        model_of_measure = {
            measure["name"]: sm["name"]
            for sm in manifest.get("semantic_models", [])
            for measure in sm.get("measures", [])
        }

        def source_model(m: dict[str, Any], depth: int = 0) -> str | None:
            """指標の元になる fct。ratio / derived / cumulative は元の指標から辿る。"""
            params = m.get("type_params") or {}
            # latest spec の simple metric は measure を持たず、集計の元を直接持つ
            agg = params.get("metric_aggregation_params") or {}
            measure = params.get("measure") or {}
            model = agg.get("semantic_model") or model_of_measure.get(measure.get("name", ""))
            if model or depth > 5:
                return model
            inputs = [params.get("numerator"), params.get("denominator")]
            inputs += params.get("metrics") or []
            inputs.append((params.get("cumulative_type_params") or {}).get("metric"))
            for ref in inputs:
                name = ref.get("name") if isinstance(ref, dict) else ref
                if name in by_name and (found := source_model(by_name[name], depth + 1)):
                    return found
            return None

        try:
            mtime = (self.warehouse / "target" / "semantic_manifest.json").stat().st_mtime
        except OSError:
            errors.append(
                "Semantic Layer の定義を読めませんでした（make dbt-build を実行してください）"
            )
            return []
        names = [m["name"] for m in manifest.get("metrics", [])]
        dims: dict[str, list[str] | Exception] = {}
        with self._lock, ThreadPoolExecutor(MF_PARALLEL) as pool:
            for name, future in [(n, pool.submit(self._dimensions_for, n, mtime)) for n in names]:
                try:
                    dims[name] = future.result()
                except (RuntimeError, subprocess.TimeoutExpired) as e:
                    dims[name] = e

        result = []
        for m in manifest.get("metrics", []):
            info = MetricInfo(
                name=m["name"],
                label=m.get("label") or m["name"],
                description=m.get("description") or "",
                type=m.get("type", ""),
                model=source_model(m),
                beam=by_metric.get(m["name"]),
            )
            found = dims.get(m["name"], [])
            if isinstance(found, Exception):
                errors.append(str(found))
                found = []
            info.dimensions = [
                Dimension(
                    name=d,
                    label=labels.get(d, (_time_label(d), ""))[0],
                    description=labels.get(d, ("", ""))[1],
                    kind="time" if d == "metric_time" or d in time_dims else "categorical",
                    entity=d.split("__", 1)[0] if "__" in d else None,
                )
                for d in found
            ]
            result.append(info)
        return result

    # --- モデル・ソース・BEAM ------------------------------------------------------------------

    def models(self, errors: list[str]) -> list[ModelInfo]:
        path = self.warehouse / "target" / "manifest.json"
        if not path.exists():
            errors.append("dbt の manifest がありません（make dbt-build を実行してください）")
            return []
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            errors.append("dbt の manifest を読めませんでした（make dbt-build を実行してください）")
            return []
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
                        ColumnInfo(name=c.name, description=c.description, data_type=c.data_type)
                        for c in self.catalog.columns(t.qualified)
                    ],
                )
                for t in tables
            ]
        except CatalogUnavailable as e:  # DB が使用中など。一覧の他の部分は返す
            errors.append(f"元データの一覧を取得できませんでした: {e}")
        except Exception as e:
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
