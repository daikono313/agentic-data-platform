"""BEAM 成果物 → 物理設計。docs/modeling-conventions.md の生成ルールを決定的に適用する。

ソーステーブル間の結合は、カタログの外部キー候補（多対一）から求める。求められないものは
推測せず、blocker として確認に回す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from adp.beam import (
    Availability,
    BeamModel,
    BusinessEvent,
    Dimension,
    FactType,
    SourceRef,
    W,
)
from adp.design.models import (
    Column,
    ColumnRole,
    DesignIssue,
    DimTable,
    FactTable,
    IssueSeverity,
    Join,
    PhysicalDesign,
    SourceTable,
)
from adp.ontology.models import Ontology


@dataclass(frozen=True)
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


class SchemaInfo(Protocol):
    """ソースのスキーマ情報。本番は Catalog、テストは辞書で実装する。"""

    def columns(self, table: str) -> list[str]: ...

    def foreign_keys(self, table: str) -> list[ForeignKey]: ...


def staging_model(table: str) -> str:
    return "stg_" + table.rpartition(".")[2]


def singular(name: str) -> str:
    """fct の名詞（複数形）から主 entity 名を作る（order_lines → order_line）。"""
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith(("ses", "xes", "ches", "shes")):
        return name[:-2]
    return name[:-1] if name.endswith("s") else name


def is_masterless(d: Dimension) -> bool:
    """マスタなしのディメンション（自然キーとイベント側のキーが同じ列）。"""
    return d.w != W.WHEN and d.business_key is not None and d.business_key == d.fact_key


_SQL_WORDS = {"as", "and", "or", "not", "null", "is", "in", "case", "when", "then", "else", "end"}
_STRING = re.compile(r"'(?:[^']|'')*'")


class JoinError(Exception):
    """結合経路が見つからない、または一意に決まらない。"""


class _Sources:
    """1 つのテーブル（fct か dim）の FROM / JOIN を組み立てる。

    結合は常に LEFT JOIN（行を落とさない）。経路はベーステーブルからの多対一の最短経路で、
    最短経路が複数ある場合は推測せずエラーにする。
    """

    def __init__(self, base_table: str, schema: SchemaInfo) -> None:
        self.schema = schema
        self.base = SourceTable(table=base_table, model=staging_model(base_table), alias="t0")
        self.aliases: dict[str, str] = {base_table: "t0"}
        self.joins: list[Join] = []

    def alias(self, table: str) -> str:
        """table への結合を（必要なら多段で）追加して別名を返す。"""
        if table in self.aliases:
            return self.aliases[table]
        for src, fk in self._path(table):
            if fk.ref_table in self.aliases:
                continue
            alias = f"t{len(self.aliases)}"
            self.aliases[fk.ref_table] = alias
            self.joins.append(
                Join(
                    left_alias=self.aliases[src],
                    left_column=fk.column,
                    right=SourceTable(
                        table=fk.ref_table, model=staging_model(fk.ref_table), alias=alias
                    ),
                    right_column=fk.ref_column,
                )
            )
        return self.aliases[table]

    def reachable(self, target: str) -> bool:
        try:
            self._path(target)
        except JoinError:
            return False
        return True

    def _path(self, target: str) -> list[tuple[str, ForeignKey]]:
        """ベーステーブルから target までの多対一の最短経路。無いか複数あれば JoinError。"""
        base = self.base.table
        if target == base:
            return []
        frontier: dict[str, list[list[tuple[str, ForeignKey]]]] = {base: [[]]}
        seen = {base}
        while frontier:
            nxt: dict[str, list[list[tuple[str, ForeignKey]]]] = {}
            for table, paths in frontier.items():
                for fk in self.schema.foreign_keys(table):
                    if fk.ref_table in seen:
                        continue
                    nxt.setdefault(fk.ref_table, []).extend([*p, (table, fk)] for p in paths)
            if target in nxt:
                paths = nxt[target]
                if len(paths) > 1:
                    routes = [" → ".join([base] + [f.ref_table for _, f in p]) for p in paths]
                    raise JoinError(
                        f"{base} から {target} への結合経路が複数あります（{' / '.join(routes)}）"
                    )
                return paths[0]
            seen.update(nxt)
            frontier = nxt
        raise JoinError(f"{base} から {target} を結合する経路が見つかりません")

    def expr(self, ref: SourceRef) -> str:
        """SourceRef を、別名で修飾した SQL 式にする。"""
        alias = self.alias(ref.table)
        if ref.column is not None:
            return f"{alias}.{ref.column}"
        assert ref.expr is not None
        own = set(self.schema.columns(ref.table))
        others = {c for t in self.aliases if t != ref.table for c in self.schema.columns(t)} - own

        def qualify(code: str) -> str:
            def sub(m: re.Match[str]) -> str:
                word = m.group(1)
                before = code[: m.start()].rstrip().lower()
                if word.lower() in _SQL_WORDS or before.endswith((" as", "(as")):
                    return word
                if word in own:
                    return f"{alias}.{word}"
                if word in others:
                    raise JoinError(
                        f"式 {ref.expr} に {ref.table} 以外の列 {word} が含まれています"
                    )
                return word

            return re.sub(r"(?<![\w.])([A-Za-z_]\w*)(?![\w(])", sub, code)

        # 文字列リテラルの中は書き換えない
        parts, last = [], 0
        for m in _STRING.finditer(ref.expr):
            parts += [qualify(ref.expr[last : m.start()]), m.group(0)]
            last = m.end()
        parts.append(qualify(ref.expr[last:]))
        return "".join(parts)


def dim_key_expr(expr: str) -> str:
    """dim_key マクロの呼び出し。式に引用符が含まれても壊れないようにする。"""
    quote = '"' if "'" in expr else "'"
    return "{{ dim_key(" + quote + expr + quote + ") }}"


class DesignBuilder:
    def __init__(
        self,
        schema: SchemaInfo,
        existing_dims: dict[str, set[str]] | None = None,
        ontology: Ontology | None = None,
    ):
        self.schema = schema
        self.existing_dims = existing_dims or {}
        self.ontology = ontology
        """あれば、ディメンションの名前がオントロジーの概念の dim と合うかを確かめる（#69）。"""
        self.issues: list[DesignIssue] = []

    def _issue(self, severity: IssueSeverity, message: str, ref: str | None = None) -> None:
        self.issues.append(DesignIssue(severity=severity, message=message, ref=ref))

    def _blocker(self, message: str, ref: str | None = None) -> None:
        self._issue(IssueSeverity.BLOCKER, message, ref)

    def build(self, beam: BeamModel) -> PhysicalDesign:
        self.issues = []
        facts = [f for e in beam.events if (f := self._fact(e)) is not None]
        dims = self._dims(beam)
        self._check_ontology(dims)
        design = PhysicalDesign(
            beam=beam.name,
            facts=facts,
            dims=dims,
            uses_dim_date=any(d.w == W.WHEN for e in beam.events for d in e.dimensions),
            issues=self.issues,
        )
        self._check_metrics(beam, design)
        design.issues = list(self.issues)
        return design

    def _check_ontology(self, dims: list[DimTable]) -> None:
        """同じ源泉の概念に dim があるのに、別の名前の dim を作ろうとしていないか。

        別の名前にすると共通ディメンション（conformed dimension）を再利用できず、同じものの
        dim が 2 つできる。ディメンションの名前を概念の dim に合わせるよう促す。
        """
        if self.ontology is None:
            return
        by_source = {c.source: c for c in self.ontology.concepts if c.kind == "entity" and c.model}
        for dim in dims:
            concept = by_source.get(dim.base.table)
            if concept is not None and concept.model != dim.name:
                name = (concept.model or "").removeprefix("dim_")
                self._blocker(
                    f"{dim.name} の源泉 {dim.base.table} は、オントロジーでは"
                    f"概念「{concept.label}」（{concept.model}）です。"
                    f"ディメンションの名前を {name} にして、{concept.model} を再利用してください",
                    dim.dimension,
                )

    # --- fct -------------------------------------------------------------------------------

    def _grain_tables(self, event: BusinessEvent) -> list[str]:
        tables = []
        for key in event.grain_keys:
            if (dg := event.degenerate(key)) is not None:
                tables.append(dg.source.table)
            elif (d := event.dimension(key)) is not None and d.fact_key is not None:
                tables.append(d.fact_key.table)
        return tables

    def _base_table(self, event: BusinessEvent) -> str | None:
        """粒度のキーのテーブルのうち、他のすべてへ多対一で結合できるもの（最も細かいテーブル）。"""
        tables = list(dict.fromkeys(self._grain_tables(event)))
        for candidate in tables:
            src = _Sources(candidate, self.schema)
            if all(src.reachable(t) for t in tables):
                return candidate
        return None

    def _fact(self, event: BusinessEvent) -> FactTable | None:
        p = f"[{event.name}]"
        if event.fact_type == FactType.PERIODIC_SNAPSHOT:
            self._blocker(f"{p} 定期スナップショット（recurring）は MVP では生成できません")
            return None
        base = self._base_table(event)
        if base is None or not event.grain:
            self._blocker(f"{p} 粒度（grain_keys）からベースのテーブルを決められません")
            return None
        src = _Sources(base, self.schema)
        columns: list[Column] = []

        def resolve(ref: SourceRef, what: str, beam_ref: str) -> str | None:
            try:
                return src.expr(ref)
            except JoinError as e:
                self._blocker(f"{p} {what}: {e}", beam_ref)
                return None

        for dg in event.degenerate_dimensions:
            if (expr := resolve(dg.source, f"縮退ディメンション {dg.name}", dg.name)) is not None:
                columns.append(
                    Column(
                        name=dg.name,
                        role=ColumnRole.DEGENERATE,
                        expr=expr,
                        description=f"{dg.label}（縮退ディメンション）",
                        source_ref=dg.name,
                    )
                )

        agg_time: tuple[str, Dimension] | None = None
        for d in event.dimensions:
            if d.fact_key is None:
                self._blocker(f"{p} {d.ref} の fact_key がありません", d.ref)
                continue
            expr = resolve(d.fact_key, f"ディメンション {d.ref}", d.ref)
            if expr is None:
                continue
            if d.w == W.WHEN:
                base_name = _time_base(d)
                is_agg = d.ref == event.agg_time_dimension or (
                    event.agg_time_dimension is None and agg_time is None
                )
                milestone = event.fact_type == FactType.ACCUMULATING_SNAPSHOT and not is_agg
                columns.append(
                    Column(
                        name=f"{base_name}_on",
                        role=ColumnRole.TIME,
                        expr=f"cast({expr} as date)",
                        description=d.label,
                        references="dim_date.date_day",
                        nullable=milestone,
                        source_ref=d.ref,
                    )
                )
                columns.append(
                    Column(
                        name=f"{base_name}_at",
                        role=ColumnRole.TIMESTAMP,
                        expr=expr,
                        description=f"{d.label}（日時）",
                        nullable=True,
                        source_ref=d.ref,
                    )
                )
                if is_agg:
                    agg_time = (f"{base_name}_on", d)
            elif is_masterless(d):
                columns.append(
                    Column(
                        name=d.ref,
                        role=ColumnRole.DEGENERATE,
                        expr=expr,
                        description=f"{d.label}（マスタなし）",
                        nullable=True,
                        source_ref=d.ref,
                    )
                )
            else:
                columns.append(
                    Column(
                        name=f"{d.ref}_key",
                        role=ColumnRole.FOREIGN_KEY,
                        expr=dim_key_expr(expr),
                        description=d.label,
                        references=f"dim_{d.name}.{d.name}_key",
                        source_ref=d.ref,
                    )
                )

        for m in event.measures:
            if m.availability != Availability.AVAILABLE or m.source is None:
                self._issue(
                    IssueSeverity.WARNING,
                    f"{p} measure {m.name} はデータが無いため作りません",
                    m.name,
                )
                continue
            if (expr := resolve(m.source, f"measure {m.name}", m.name)) is not None:
                columns.append(
                    Column(
                        name=m.name,
                        role=ColumnRole.MEASURE,
                        expr=expr,
                        description=m.label + (f"（{m.unit}）" if m.unit else ""),
                        nullable=True,
                        source_ref=m.name,
                    )
                )

        names = [c.name for c in columns]
        for dup in sorted({n for n in names if names.count(n) > 1}):
            self._blocker(f"{p} fct の列名 {dup} が重複します", dup)

        grain_columns = []
        for key in event.grain_keys:
            if event.degenerate(key):
                grain_columns.append(key)
            elif (gd := event.dimension(key)) is not None:
                if gd.w == W.WHEN:
                    grain_columns.append(f"{_time_base(gd)}_on")
                else:
                    grain_columns.append(gd.ref if is_masterless(gd) else f"{gd.ref}_key")
        table_name = event.table_name or event.name
        return FactTable(
            name=event.fact_table,
            event=event.name,
            label=event.label,
            fact_type=event.fact_type,
            grain=event.grain,
            grain_columns=grain_columns,
            base=src.base,
            joins=src.joins,
            columns=columns,
            primary_entity=singular(table_name),
            agg_time_column=agg_time[0] if agg_time else None,
            agg_time_grain=agg_time[1].time_grain if agg_time else None,
        )

    # --- dim -------------------------------------------------------------------------------

    def _dims(self, beam: BeamModel) -> list[DimTable]:
        by_name: dict[str, list[Dimension]] = {}
        for event in beam.events:
            for d in event.dimensions:
                if d.w != W.WHEN and not is_masterless(d):
                    by_name.setdefault(d.name, []).append(d)
        return [dim for name, uses in by_name.items() if (dim := self._dim(name, uses))]

    def _dim(self, name: str, uses: list[Dimension]) -> DimTable | None:
        first = next((d for d in uses if d.role is None), uses[0])
        if first.business_key is None:
            self._blocker(f"dim_{name} の自然キーがありません", name)
            return None
        for d in uses:
            if d.business_key != first.business_key:
                self._blocker(
                    f"dim_{name} の自然キーが使われ方によって異なります（{d.ref}）", d.ref
                )
        src = _Sources(first.business_key.table, self.schema)
        try:
            natural = src.expr(first.business_key)
        except JoinError as e:
            self._blocker(f"dim_{name} の自然キー: {e}", name)
            return None
        columns = [
            Column(
                name=first.business_key.column or f"{name}_natural_key",
                role=ColumnRole.NATURAL_KEY,
                expr=natural,
                description=f"{first.label}の自然キー",
                nullable=True,
            )
        ]
        sources: dict[str, SourceRef] = {}
        for d in uses:
            for a in d.attributes:
                ref = f"{d.ref}.{a.name}"
                if a.name in sources:
                    if a.source is not None and a.source != sources[a.name]:
                        self._issue(
                            IssueSeverity.WARNING,
                            f"dim_{name}.{a.name} のソースが使われ方によって異なります。"
                            "最初のものを使います",
                            ref,
                        )
                    continue
                if a.availability != Availability.AVAILABLE or a.source is None:
                    self._issue(
                        IssueSeverity.WARNING,
                        f"dim_{name}.{a.name} はデータが無いため作りません",
                        ref,
                    )
                    continue
                try:
                    expr = src.expr(a.source)
                except JoinError as e:
                    self._blocker(f"dim_{name}.{a.name}: {e}", ref)
                    continue
                if a.history_required:
                    self._issue(
                        IssueSeverity.WARNING,
                        f"dim_{name}.{a.name} は履歴が必要だが、MVP では上書き（Type1）で作る",
                        ref,
                    )
                sources[a.name] = a.source
                columns.append(
                    Column(
                        name=a.name,
                        role=ColumnRole.ATTRIBUTE,
                        expr=expr,
                        description=a.label,
                        nullable=True,
                        source_ref=ref,
                    )
                )
        existing = self.existing_dims.get(f"dim_{name}")
        return DimTable(
            name=f"dim_{name}",
            dimension=name,
            label=first.label,
            key_column=f"{name}_key",
            base=src.base,
            joins=src.joins,
            columns=columns,
            hierarchy=list(first.hierarchy),
            roles=sorted({d.role for d in uses if d.role}),
            exists=existing is not None,
            new_columns=[
                c.name for c in columns if existing is not None and c.name not in existing
            ],
        )

    # --- metric ----------------------------------------------------------------------------

    def _check_metrics(self, beam: BeamModel, design: PhysicalDesign) -> None:
        """指標が使う measure と切り口が、設計に列として存在するか。"""
        facts = {f.event: f for f in design.facts}
        dims = {d.dimension: d for d in design.dims}
        for metric in beam.metrics:
            fact = facts.get(metric.event)
            event = beam.event(metric.event)
            if fact is None or event is None:
                continue
            p = f"[metric {metric.name}]"
            if metric.measure and fact.column(metric.measure) is None:
                self._blocker(f"{p} measure {metric.measure} が {fact.name} にありません")
            for ref in metric.group_by:
                dim_ref, _, attr = ref.partition(".")
                d = event.dimension(dim_ref)
                if event.degenerate(ref) or (d is not None and d.w == W.WHEN):
                    continue
                if d is None:
                    self._blocker(f"{p} 切り口 {ref} がイベントにありません", ref)
                elif is_masterless(d):
                    if fact.column(d.ref) is None:
                        self._blocker(f"{p} 切り口 {ref} の列が {fact.name} にありません", ref)
                elif (dim := dims.get(d.name)) is None or all(c.name != attr for c in dim.columns):
                    self._blocker(f"{p} 切り口 {ref} の列が dim_{d.name} にありません", ref)


def _time_base(d: Dimension) -> str:
    """When の列名の基になる名前（ordered_at → ordered）。"""
    if d.fact_key is not None and d.fact_key.column and d.fact_key.column.endswith("_at"):
        return d.fact_key.column.removesuffix("_at")
    return d.ref
