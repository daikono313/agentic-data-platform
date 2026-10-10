"""物理設計（スタースキーマ）の中間表現。BEAM 成果物から作り、dbt と Semantic Layer の生成に使う。

規約は docs/modeling-conventions.md の「BEAM 成果物との対応（生成ルール）」。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from adp.beam import FactType, TimeGrain


class ColumnRole(StrEnum):
    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    DEGENERATE = "degenerate"
    """縮退ディメンション、またはマスタなしのディメンション。"""
    MEASURE = "measure"
    TIME = "time"
    """When の日付列（dim_date を参照）。"""
    TIMESTAMP = "timestamp"
    """When の日時列。"""
    NATURAL_KEY = "natural_key"
    ATTRIBUTE = "attribute"


class SourceTable(BaseModel):
    table: str
    """ソーステーブル（例: raw.orders）。"""
    model: str
    """参照する staging モデル（例: stg_orders）。"""
    alias: str
    """SQL 内の別名。"""


class Join(BaseModel):
    """多対一の結合。`left.left_column = right.right_column`。"""

    left_alias: str
    left_column: str
    right: SourceTable
    right_column: str


class Column(BaseModel):
    name: str
    role: ColumnRole
    expr: str
    """SELECT 句の式（dbt の Jinja を含みうる。例: {{ dim_key('o.customer_id') }}）。"""
    description: str = ""
    references: str | None = None
    """FK・日付列の参照先（例: dim_customer.customer_key、dim_date.date_day）。"""
    nullable: bool = False
    source_ref: str | None = None
    """BEAM 上の参照（measure 名、`<ref>.<attr>` など）。"""


class FactTable(BaseModel):
    name: str
    event: str
    label: str
    fact_type: FactType
    grain: str
    grain_columns: list[str]
    base: SourceTable
    joins: list[Join] = Field(default_factory=list)
    columns: list[Column]
    primary_entity: str
    """MetricFlow の主 entity 名（fct の単数形）。"""
    agg_time_column: str | None = None
    agg_time_grain: TimeGrain | None = None

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


class DimTable(BaseModel):
    name: str
    dimension: str
    label: str
    key_column: str
    base: SourceTable
    joins: list[Join] = Field(default_factory=list)
    columns: list[Column]
    """自然キーと属性（主キーは key_column）。"""
    hierarchy: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    """fct で使われている役割（role 付きの参照）。"""
    exists: bool = False
    """既存の dim を再利用する（カラム追加のみ）。"""
    new_columns: list[str] = Field(default_factory=list)
    """既存の dim に追加が必要な列。"""


class IssueSeverity(StrEnum):
    BLOCKER = "blocker"
    """生成できない。ユーザーかエンジニアの確認が必要。"""
    WARNING = "warning"
    """生成はできるが PR に書く。"""


class DesignIssue(BaseModel):
    severity: IssueSeverity
    message: str
    ref: str | None = None


class PhysicalDesign(BaseModel):
    beam: str
    facts: list[FactTable]
    dims: list[DimTable]
    uses_dim_date: bool
    issues: list[DesignIssue] = Field(default_factory=list)

    @property
    def blockers(self) -> list[DesignIssue]:
        return [i for i in self.issues if i.severity == IssueSeverity.BLOCKER]
