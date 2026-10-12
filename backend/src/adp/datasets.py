"""取得可能なデータセット・指標の一覧（#38）。

ユーザーが要求を出す前に「今どんなデータが見られるか」を知るためのもの。4 つの区分で返す。

- metrics: Semantic Layer の指標と、使える切り口（すぐチャートで見られる）
- models: 整備済みの marts（dim / fct）
- sources: 元データ（ソースカタログ）
- beams: 確定した BEAM 成果物（どの要求から指標が作られたか）

切り口はプロセス内の MetricFlow に問い合わせる（`mf list dimensions` と同じ結果。DB には接続しない。
ADR 0003）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from adp.catalog.catalog import Catalog, CatalogUnavailable, TableProfile
from adp.charts import ChartError, MetricFlowCompiler
from adp.yaml_docs import load_yaml_docs


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
    unmapped: bool = False
    """オントロジーのどの概念にもなっていない（未整理。取り込みが必要。#69）。"""
    used_by: list[str] | None = None
    """この源泉を（staging などを経由して）使っている fact / dim（空なら未活用。dbt の manifest が
    無く分からなければ None。#72）。"""


class BeamInfo(BaseModel):
    name: str
    title: str
    request: str
    metrics: list[str]


class SourceProfile(TableProfile):
    used_by: list[str] = Field(default_factory=list)
    """この源泉を（staging などを経由して）使っている整備済みのモデル（dim / fct）。"""


class BusAttribute(BaseModel):
    name: str
    """MetricFlow の参照名（例: product__category）。"""
    label: str


class BusGroup(BaseModel):
    """バスマトリクスの列のまとまり。

    dimension 1 つか、fact 自身が持つ属性（退化ディメンション）。
    """

    name: str
    """dim のモデル名。fact 自身の属性なら fact のモデル名。"""
    label: str
    kind: str
    """dim / degenerate"""
    attributes: list[BusAttribute] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    """この dim の元の源泉（<schema>.<table>。#72）。"""
    candidate: bool = False
    """源泉の取り込みで増える候補（まだ無い dim。#69）。"""


class BusRow(BaseModel):
    name: str
    """fct のモデル名。"""
    label: str
    groups: list[str] = Field(default_factory=list)
    """結合できる（✓ が付く）列のまとまりの name。"""
    sources: list[str] = Field(default_factory=list)
    """この fct の元の源泉（<schema>.<table>。#72）。"""
    candidate: bool = False
    """源泉の取り込みで増える候補（まだ無い fct。#69）。"""


class BusMatrix(BaseModel):
    """Kimball のバスマトリクス（fact × dimension。#62）。

    Semantic Layer の entity の結合関係から作る。
    """

    groups: list[BusGroup] = Field(default_factory=list)
    rows: list[BusRow] = Field(default_factory=list)


class OntologyView(BaseModel):
    """オントロジーの画面用（#67）。"""

    mermaid: str
    ontology: dict[str, Any]
    unmapped_sources: list[str] = Field(default_factory=list)
    """どの概念にもなっていない源泉（未整理）。"""
    unmapped_models: list[str] = Field(default_factory=list)
    """どの概念にも対応していない dim / fct。"""
    errors: list[str] = Field(default_factory=list)
    """源泉・dbt のモデルとの食い違い。"""


class CleansingView(BaseModel):
    """源泉のクレンジングの定義（#68）。"""

    path: str
    """定義のファイル（リポジトリからの相対パス）。"""
    existing: str | None = None
    """今の定義（無ければ None）。"""
    draft: str | None = None
    """プロファイルから作った雛形（作れなければ None）。"""
    draft_error: str | None = None


class Datasets(BaseModel):
    metrics: list[MetricInfo]
    models: list[ModelInfo]
    bus: BusMatrix = Field(default_factory=BusMatrix)
    sources: list[SourceInfo]
    beams: list[BeamInfo]
    errors: list[str] = Field(default_factory=list)
    """一部が取れなかった場合の理由（一覧全体は返す）。"""


class DatasetIndex:
    def __init__(self, repo_root: Path, catalog: Catalog | None) -> None:
        self.repo_root = repo_root
        self.warehouse = repo_root / "warehouse"
        self.catalog = catalog
        self.compiler = MetricFlowCompiler(self.warehouse)
        """切り口の一覧はプロセス内の MetricFlow から取る（mf の起動を待たない。#43）。"""

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

        dims: dict[str, list[str] | Exception] = {}
        for m in manifest.get("metrics", []):
            try:
                dims[m["name"]] = self.compiler.dimensions(m["name"])
            except ChartError as e:
                dims[m["name"]] = e

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

    def _mapped_sources(self) -> set[str] | None:
        """オントロジーの概念になっている源泉。オントロジーが無い・読めなければ None。"""
        from adp.ontology import load

        try:
            return {c.source for c in load(self.repo_root / "ontology" / "ontology.yml").concepts}
        except (OSError, ValueError):
            return None

    def sources(self, errors: list[str]) -> list[SourceInfo]:
        if self.catalog is None:
            return []
        mapped = self._mapped_sources()
        manifest = self._manifest()
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
                    unmapped=mapped is not None and t.qualified not in mapped,
                    used_by=None
                    if manifest is None
                    else self._used_by(t.schema_name, t.name, manifest),
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
        docs = load_yaml_docs(self.repo_root / "beam", "*.yml")
        failed = [p.path for p in docs.problems]
        for doc in docs.docs:
            try:
                data = doc.data
                result.append(
                    BeamInfo(
                        name=data["name"],
                        title=data.get("title", data["name"]),
                        request=data.get("request", ""),
                        metrics=[m["name"] for m in data.get("metrics", [])],
                    )
                )
            except Exception:
                failed.append(doc.path)
        errors.extend(f"{p.name} を読めませんでした" for p in sorted(failed))
        return result

    def profile(self, table: str) -> SourceProfile:
        """源泉 1 つのデータプロファイル（#62）。

        対象外のテーブルは ValueError、DB が使用中なら CatalogUnavailable。
        """
        if self.catalog is None:
            raise CatalogUnavailable("元データのカタログが使えません")
        profile = self.catalog.profile_table(table, detail=True)
        return SourceProfile(
            **profile.model_dump(), used_by=self._used_by(profile.schema_name, profile.name)
        )

    def _manifest(self) -> dict[str, Any] | None:
        try:
            data: dict[str, Any] = json.loads(
                (self.warehouse / "target" / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        return data

    def _used_by(self, schema: str, name: str, manifest: dict[str, Any] | None = None) -> list[str]:
        manifest = manifest if manifest is not None else self._manifest()
        if manifest is None:
            return []
        children: dict[str, list[str]] = manifest.get("child_map", {})
        starts = [
            k
            for k, v in manifest.get("sources", {}).items()
            if v.get("schema") == schema and (v.get("identifier") or v.get("name")) == name
        ]
        seen: set[str] = set()
        stack = list(starts)
        while stack:
            for child in children.get(stack.pop(), []):
                if child.startswith(("model.", "snapshot.")) and child not in seen:
                    seen.add(child)
                    stack.append(child)
        # ノードの名前で（versioned model の「.v1」などを名前と取り違えない）
        nodes = manifest.get("nodes", {})
        models = {nodes.get(c, {}).get("name") or c.rsplit(".", 1)[-1] for c in seen}
        return sorted(m for m in models if m.startswith(("dim_", "fct_")))

    def bus_matrix(self, manifest: dict[str, Any] | None = None) -> BusMatrix:
        """entity の結合関係から fact × dimension の表を作る。

        fct の foreign entity が、dim の primary / unique entity と一致すれば結合できる
        （MetricFlow と同じく entity 名で結合する）。
        dim の foreign entity の先の dim（1 段の snowflake）も含める。
        role-playing（別名の entity）は別の切り口として扱われ、ここでは ✓ にしない。
        semantic manifest が無ければ空（理由は metrics() が errors に入れる）。
        """
        manifest = manifest if manifest is not None else self._semantic_manifest()
        if manifest is None:
            return BusMatrix()
        models = sorted(
            (m for m in manifest.get("semantic_models", []) if m.get("name")),
            key=lambda m: m["name"],
        )

        def entities(sm: dict[str, Any], *types: str) -> list[str]:
            return [e["name"] for e in sm.get("entities", []) if e.get("type") in types]

        def primary(sm: dict[str, Any]) -> str | None:
            found = entities(sm, "primary")
            return found[0] if found else sm.get("primary_entity")

        def attributes(sm: dict[str, Any], entity: str) -> list[BusAttribute]:
            return [
                BusAttribute(
                    name=f"{entity}__{d['name']}",
                    label=d.get("label") or _short(d.get("description") or "") or d["name"],
                )
                for d in sm.get("dimensions", [])
                if d.get("name")
            ]

        groups: list[BusGroup] = []
        dims_of_entity: dict[str, list[str]] = {}
        dim_foreign: dict[str, list[str]] = {}
        for sm in models:
            entity = primary(sm)
            if not sm["name"].startswith("dim_") or entity is None:
                continue
            for e in [entity, *entities(sm, "unique")]:
                dims_of_entity.setdefault(e, []).append(sm["name"])
            dim_foreign[sm["name"]] = entities(sm, "foreign")
            groups.append(
                BusGroup(
                    name=sm["name"],
                    label=_dim_label(sm),
                    kind="dim",
                    attributes=attributes(sm, entity),
                )
            )
        rows: list[BusRow] = []
        for sm in models:
            if not sm["name"].startswith("fct_"):
                continue
            label = sm.get("label") or _short(sm.get("description") or "") or sm["name"]
            joined = [d for e in entities(sm, "foreign") for d in dims_of_entity.get(e, [])]
            joined += [
                d for j in list(joined) for e in dim_foreign[j] for d in dims_of_entity.get(e, [])
            ]
            entity = primary(sm)
            # primary entity の無い fct の属性は MetricFlow から参照できないので出さない
            own = attributes(sm, entity) if entity else []
            if own:
                groups.append(
                    BusGroup(name=sm["name"], label=label, kind="degenerate", attributes=own)
                )
                joined.append(sm["name"])
            rows.append(BusRow(name=sm["name"], label=label, groups=list(dict.fromkeys(joined))))
        # dim を先、fact 自身の属性を後ろに並べる
        groups.sort(key=lambda g: g.kind != "dim")
        return BusMatrix(groups=groups, rows=rows)

    def _bus(self, errors: list[str]) -> BusMatrix:
        try:
            bus = self.bus_matrix()
            manifest = self._manifest()
            if manifest is not None:  # fact / dim ごとの元の源泉（どの源泉が使われているか。#72）
                upstream = model_sources(manifest)
                for g in bus.groups:
                    g.sources = upstream.get(g.name, [])
                for r in bus.rows:
                    r.sources = upstream.get(r.name, [])
            return bus
        except Exception as e:  # 壊れた manifest でも一覧の他の部分は返す
            errors.append(f"バスマトリクスを作れませんでした: {type(e).__name__}")
            return BusMatrix()

    def ontology(self) -> OntologyView:
        """`ontology/ontology.yml` を読み、図（Mermaid）と食い違いを返す。

        yml が無い・壊れている場合は FileNotFoundError / ValueError。
        """
        from adp.ontology import load, mermaid
        from adp.ontology.check import check

        onto = load(self.repo_root / "ontology" / "ontology.yml")
        view = OntologyView(
            mermaid=mermaid(onto),
            ontology=onto.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        if self.catalog is None:
            return view
        try:
            result = check(onto, self.catalog, self.warehouse / "target" / "manifest.json")
        except CatalogUnavailable as e:
            view.errors.append(f"源泉との照合ができませんでした: {e}")
            return view
        except Exception as e:  # DB の例外など。図は返す（本文は出さない）
            view.errors.append(f"源泉との照合ができませんでした: {type(e).__name__}")
            return view
        view.errors += result.errors
        view.unmapped_sources = result.unmapped_sources
        view.unmapped_models = result.unmapped_models
        return view

    def cleansing(self, table: str) -> CleansingView:
        """源泉のクレンジングの定義と、プロファイルからの雛形（#68）。

        対象外のテーブルは ValueError、DB が使用中なら CatalogUnavailable。
        """
        from adp.cleansing.check import draft
        from adp.cleansing.models import dump_spec, spec_path

        if self.catalog is None:
            raise CatalogUnavailable("元データのカタログが使えません")
        profile = self.catalog.profile_table(table)
        source = f"{profile.schema_name}.{profile.name}"
        path = spec_path(self.warehouse / "cleansing", source)
        view = CleansingView(
            path=str(path.relative_to(self.repo_root)),
            existing=path.read_text(encoding="utf-8") if path.exists() else None,
        )
        try:
            view.draft = dump_spec(draft(profile))
        except ValueError:  # 定義に書けない源泉の名前など。今の定義は返す
            view.draft_error = (
                "この源泉からは雛形を作れませんでした（名前に使えない文字があります）"
            )
        return view

    def all(self) -> Datasets:
        errors: list[str] = []
        beams = self.beams(errors)
        return Datasets(
            metrics=self.metrics(beams, errors),
            models=self.models(errors),
            bus=self._bus(errors),
            sources=self.sources(errors),
            beams=beams,
            errors=errors,
        )


def _short(text: str) -> str:
    """説明文の最初の句（「（」「。」まで）。表の見出しに使う。"""
    return re.split(r"[（(。\n]", text, maxsplit=1)[0].strip()


def _dim_label(sm: dict[str, Any]) -> str:
    """dim の見出し。「顧客ディメンション（Who）」→「顧客」"""
    label = sm.get("label") or _short(sm.get("description") or "")
    return label.removesuffix("ディメンション").strip() or sm["name"]


def _time_label(name: str) -> str:
    if name == "metric_time":
        return "日付（指標の時間軸）"
    return name


def model_sources(manifest: dict[str, Any]) -> dict[str, list[str]]:
    """モデル名 → そのモデルが（staging などを経由して）読む源泉（<schema>.<table>）。"""
    sources = {
        k: f"{v.get('schema')}.{v.get('identifier') or v.get('name')}"
        for k, v in manifest.get("sources", {}).items()
    }
    parents: dict[str, list[str]] = manifest.get("parent_map", {})
    result: dict[str, list[str]] = {}
    for key, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "model":
            continue
        found: set[str] = set()
        seen: set[str] = set()
        stack = [key]
        while stack:
            for p in parents.get(stack.pop(), []):
                if p in seen:
                    continue
                seen.add(p)
                if p in sources:
                    found.add(sources[p])
                elif p.startswith(("model.", "snapshot.")):
                    stack.append(p)
        result[node["name"]] = sorted(found)
    return result
