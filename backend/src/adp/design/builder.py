"""BEAM 成果物 → 物理設計。docs/modeling-conventions.md の生成ルールを決定的に適用する。

ソーステーブル間の結合は、カタログの外部キー候補（多対一）から求める。求められないものは
推測せず、blocker として確認に回す。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from adp.beam import (
    Availability,
    BeamModel,
    BusinessEvent,
    Dimension,
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


class _Sources:
    """1 つのテーブル（fct か dim）の FROM / JOIN を組み立てる。"""

    def __init__(self, base_table: str, schema: SchemaInfo) -> None:
        self.schema = schema
        self.base = SourceTable(table=base_table, model=staging_model(base_table), alias="t0")
        self.aliases: dict[str, str] = {base_table: "t0"}
        self.joins: list[Join] = []

    def alias(self, table: str) -> str | None:
        """table への結合を（必要なら多段で）追加して別名を返す。結合できなければ None。"""
        if table in self.aliases:
            return self.aliases[table]
        path = self._path(table)
        if path is None:
            return None
        for src, fk in path:
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

    def _path(self, target: str) -> list[tuple[str, ForeignKey]] | None:
        """結合済みのテーブルから target までの多対一の経路（幅優先）。"""
        queue: deque[tuple[str, list[tuple[str, ForeignKey]]]] = deque(
            (t, []) for t in self.aliases
        )
        seen = set(self.aliases)
        while queue:
            table, path = queue.popleft()
            for fk in self.schema.foreign_keys(table):
                if fk.ref_table in seen:
                    continue
                step = [*path, (table, fk)]
                if fk.ref_table == target:
                    return step
                seen.add(fk.ref_table)
                queue.append((fk.ref_table, step))
        return None

    def expr(self, ref: SourceRef) -> str | None:
        """SourceRef を、別名で修飾した SQL 式にする。"""
        alias = self.alias(ref.table)
        if alias is None:
            return None
        if ref.column is not None:
            return f"{alias}.{ref.column}"
        assert ref.expr is not None
        cols = set(self.schema.columns(ref.table))
        return re.sub(
            r"(?<![\w.])([A-Za-z_]\w*)(?![\w(])",
            lambda m: f"{alias}.{m.group(1)}" if m.group(1) in cols else m.group(1),
            ref.expr,
        )


class DesignBuilder:
    def __init__(self, schema: SchemaInfo, existing_dims: dict[str, set[str]] | None = None):
        self.schema = schema
        self.existing_dims = existing_dims or {}
        self.issues: list[DesignIssue] = []

    def _issue(self, severity: IssueSeverity, message: str, ref: str | None = None) -> None:
        self.issues.append(DesignIssue(severity=severity, message=message, ref=ref))

    def build(self, beam: BeamModel) -> PhysicalDesign:
        self.issues = []
        facts = [self._fact(e) for e in beam.events]
        dims = self._dims(beam)
        return PhysicalDesign(
            beam=beam.name,
            facts=[f for f in facts if f is not None],
            dims=dims,
            uses_dim_date=any(d.w == W.WHEN for e in beam.events for d in e.dimensions),
            issues=self.issues,
        )

    # --- fct -------------------------------------------------------------------------------

    def _base_table(self, event: BusinessEvent) -> str | None:
        for key in event.grain_keys:
            if (dg := event.degenerate(key)) is not None:
                return dg.source.table
            if (d := event.dimension(key)) is not None and d.fact_key is not None:
                return d.fact_key.table
        return None

    def _fact(self, event: BusinessEvent) -> FactTable | None:
        p = f"[{event.name}]"
        base = self._base_table(event)
        if base is None or not event.grain:
            self._issue(
                IssueSeverity.BLOCKER, f"{p} 粒度（grain_keys）からベースのテーブルを決められません"
            )
            return None
        src = _Sources(base, self.schema)
        columns: list[Column] = []

        def add(column: Column | None, ref: str, what: str, table: str) -> None:
            if column is None:
                self._issue(
                    IssueSeverity.BLOCKER,
                    f"{p} {what} のソース {table} を {base} から結合する経路が見つかりません",
                    ref,
                )
            else:
                columns.append(column)

        for dg in event.degenerate_dimensions:
            expr = src.expr(dg.source)
            add(
                None
                if expr is None
                else Column(
                    name=dg.name,
                    role=ColumnRole.DEGENERATE,
                    expr=expr,
                    description=f"{dg.label}（縮退ディメンション）",
                    source_ref=dg.name,
                ),
                dg.name,
                f"縮退ディメンション {dg.name}",
                dg.source.table,
            )

        agg_time: tuple[str, Dimension] | None = None
        for d in event.dimensions:
            if d.fact_key is None:
                self._issue(IssueSeverity.BLOCKER, f"{p} {d.ref} の fact_key がありません", d.ref)
                continue
            expr = src.expr(d.fact_key)
            what = f"ディメンション {d.ref}"
            if expr is None:
                add(None, d.ref, what, d.fact_key.table)
                continue
            if d.w == W.WHEN:
                base_name = _time_base(d)
                columns.append(
                    Column(
                        name=f"{base_name}_on",
                        role=ColumnRole.TIME,
                        expr=f"cast({expr} as date)",
                        description=d.label,
                        references="dim_date.date_day",
                        nullable=event.fact_type.value == "accumulating_snapshot",
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
                if agg_time is None or d.ref == event.agg_time_dimension:
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
                        expr="{{ dim_key('" + expr + "') }}",
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
            expr = src.expr(m.source)
            add(
                None
                if expr is None
                else Column(
                    name=m.name,
                    role=ColumnRole.MEASURE,
                    expr=expr,
                    description=m.label + (f"（{m.unit}）" if m.unit else ""),
                    nullable=True,
                    source_ref=m.name,
                ),
                m.name,
                f"measure {m.name}",
                m.source.table,
            )

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
        dims = []
        for name, uses in by_name.items():
            dim = self._dim(name, uses)
            if dim is not None:
                dims.append(dim)
        return dims

    def _dim(self, name: str, uses: list[Dimension]) -> DimTable | None:
        first = next((d for d in uses if d.role is None), uses[0])
        if first.business_key is None:
            self._issue(IssueSeverity.BLOCKER, f"dim_{name} の自然キーがありません", name)
            return None
        src = _Sources(first.business_key.table, self.schema)
        natural = src.expr(first.business_key)
        assert natural is not None
        columns = [
            Column(
                name=first.business_key.column or f"{name}_natural_key",
                role=ColumnRole.NATURAL_KEY,
                expr=natural,
                description=f"{first.label}の自然キー",
                nullable=True,
            )
        ]
        seen = {columns[0].name}
        for d in uses:
            for a in d.attributes:
                if a.name in seen:
                    continue
                if a.availability != Availability.AVAILABLE or a.source is None:
                    self._issue(
                        IssueSeverity.WARNING,
                        f"dim_{name}.{a.name} はデータが無いため作りません",
                        f"{d.ref}.{a.name}",
                    )
                    continue
                expr = src.expr(a.source)
                if expr is None:
                    self._issue(
                        IssueSeverity.BLOCKER,
                        f"dim_{name}.{a.name} のソース {a.source.table} を"
                        f" {first.business_key.table} から結合する経路が見つかりません",
                        f"{d.ref}.{a.name}",
                    )
                    continue
                if a.history_required:
                    self._issue(
                        IssueSeverity.WARNING,
                        f"dim_{name}.{a.name} は履歴が必要だが、MVP では上書き（Type1）で作る",
                        f"{d.ref}.{a.name}",
                    )
                seen.add(a.name)
                columns.append(
                    Column(
                        name=a.name,
                        role=ColumnRole.ATTRIBUTE,
                        expr=expr,
                        description=a.label,
                        nullable=True,
                        source_ref=f"{d.ref}.{a.name}",
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


def _time_base(d: Dimension) -> str:
    """When の列名の基になる名前（ordered_at → ordered）。"""
    if d.fact_key is not None and d.fact_key.column and d.fact_key.column.endswith("_at"):
        return d.fact_key.column.removesuffix("_at")
    return d.ref
