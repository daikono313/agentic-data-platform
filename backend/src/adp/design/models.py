"""物理設計（スタースキーマ）の中間表現。BEAM 成果物から作り、dbt と Semantic Layer の生成に使う。

規約は docs/modeling-conventions.md の「BEAM 成果物との対応（生成ルール）」。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from adp.beam import FactType, StateMethod, TimeGrain


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
    """多対一の結合。`left.left_column = right.right_column`。

    常に LEFT JOIN で生成する（外部キーが NULL の行や、参照先が無い行を落とさないため）。
    """

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


class SnapshotDesign(BaseModel):
    """定期スナップショットの fct の作り方（ADR 0008）。SQL は `adp.generation.dbt` が作る。

    記録を区切り（粒度のキーから When を除いた列）ごとに時刻の順にたどり、期間（grain）の終わり
    ごとの値にする。記録の無い期間は前の期間の値を引き継ぐ。
    """

    time_column: str
    """期間の終わりの日付の列（dim_date を参照）。"""
    recorded_at: str
    """記録の時刻の式（ソースの When の列）。"""
    grain: TimeGrain
    methods: dict[str, StateMethod]
    """measure の列 → 値の求め方。"""
    resets: dict[str, str] = Field(default_factory=dict)
    """running_total の measure の列 → 値を置き換える行の条件（ソースの列の SQL）。"""


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
    snapshot: SnapshotDesign | None = None
    """定期スナップショット（periodic_snapshot）のときの作り方。"""

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def column_for(self, ref: str, role: ColumnRole) -> Column | None:
        """BEAM の参照（When の ref など）と役割に対応する列（例: When の日付列・日時列）。"""
        return next((c for c in self.columns if c.source_ref == ref and c.role == role), None)


class HistoryDesign(BaseModel):
    """当時の値で見たい属性（BEAM の `history_required`）の、dbt snapshot と履歴モデル（SCD Type 2）
    の作り方（ADR 0009）。SQL・YAML は `adp.generation.dbt.history_files` が作る。

    snapshot はクレンジング層（stg）の行の版を記録し、履歴モデルは版ごとに、dim と同じキー・履歴の
    属性・期間（valid_from 以上 valid_to 未満）を持つ。既存の dim と fct は変えない（R15）。
    """

    model: str
    """履歴モデルの名前（`dim_<name>_history`）。"""
    snapshot: str
    """snapshot の名前（`snp_<源泉の表>`）。"""
    natural_key: str
    """源泉の自然キーの列（dim の自然キーと同じ）。"""
    columns: list[Column]
    """履歴の属性（expr は源泉の列名。名前・説明は dim の属性と同じ）。"""


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
    history: HistoryDesign | None = None
    """当時の値で見たい属性があるときの、snapshot と履歴モデル（ADR 0009）。"""


class ExistingFact(BaseModel):
    """既存の fct の種類と粒度（確定した BEAM 成果物から。#113）。"""

    fact_type: FactType | None
    """BEAM 成果物どうしで食い違うときは None（conflict に理由）。"""
    grain_sources: frozenset[str] = frozenset()
    """粒度のキーの源泉（raw.order_items.order_item_id など）。"""
    beams: list[str] = Field(default_factory=list)
    conflict: str | None = None


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
