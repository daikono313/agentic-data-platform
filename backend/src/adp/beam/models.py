"""BEAM✲ 成果物の型。インタビュー・UI・コード生成が共通して使う正本。

用語は docs/knowledge/beam.md を参照。成果物はインタビュー中は未完成（draft）で、
`finalize_issues()` が空になったときだけ確定できる。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


def _snake(v: str) -> str:
    if not SNAKE_CASE.match(v):
        raise ValueError(f"snake_case の英小文字で指定してください: {v!r}")
    return v


Identifier = Annotated[str, AfterValidator(_snake)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class W(StrEnum):
    """How many 以外の 6W。How many は measure として別に持つ。"""

    WHO = "who"
    WHAT = "what"
    WHEN = "when"
    WHERE = "where"
    WHY = "why"
    HOW = "how"


class StoryType(StrEnum):
    """イベントの種類。fct の種類を決める。"""

    DISCRETE = "discrete"
    RECURRING = "recurring"
    EVOLVING = "evolving"


class FactType(StrEnum):
    TRANSACTION = "transaction"
    PERIODIC_SNAPSHOT = "periodic_snapshot"
    ACCUMULATING_SNAPSHOT = "accumulating_snapshot"


FACT_TYPE_BY_STORY: dict[StoryType, FactType] = {
    StoryType.DISCRETE: FactType.TRANSACTION,
    StoryType.RECURRING: FactType.PERIODIC_SNAPSHOT,
    StoryType.EVOLVING: FactType.ACCUMULATING_SNAPSHOT,
}


class Aggregation(StrEnum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    AVERAGE = "average"
    MIN = "min"
    MAX = "max"


class Additivity(StrEnum):
    """足し上げてよいか。単価は non_additive、残高は時間方向に semi_additive。"""

    ADDITIVE = "additive"
    SEMI_ADDITIVE = "semi_additive"
    NON_ADDITIVE = "non_additive"


class Availability(StrEnum):
    """ソースデータで作れるか。"""

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class TimeGrain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class Level(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SourceRef(_Base):
    """ソースのどこから作るか。column か expr のどちらかを指定する。"""

    table: str = Field(description="ソーステーブル（例: raw.orders）")
    column: str | None = None
    expr: str | None = Field(default=None, description="column で表せない場合の SQL 式")

    @model_validator(mode="after")
    def _column_or_expr(self) -> SourceRef:
        if (self.column is None) == (self.expr is None):
            raise ValueError("column と expr のどちらか一方を指定してください")
        return self


class Measure(_Base):
    """How many。fct の数値カラムになる。"""

    name: Identifier
    label: str = Field(description="表示名（日本語）")
    description: str = ""
    aggregation: Aggregation = Aggregation.SUM
    additivity: Additivity = Additivity.ADDITIVE
    unit: str | None = Field(default=None, description="単位（円、個 など）")
    source: SourceRef | None = None
    availability: Availability = Availability.UNKNOWN


class Attribute(_Base):
    """ディメンションの属性。dim のカラムになる。"""

    name: Identifier
    label: str
    description: str = ""
    examples: list[str] = Field(default_factory=list)
    source: SourceRef | None = None
    availability: Availability = Availability.UNKNOWN
    history_required: bool = Field(
        default=False, description="値の変更履歴が必要か（SCD Type2 の要否。MVP では記録のみ）"
    )


class Dimension(_Base):
    """6W のいずれかに対応するディメンション。"""

    w: W
    name: Identifier = Field(description="ディメンション名（例: customer）。dim_<name> になる")
    label: str
    description: str = ""
    role: Identifier | None = Field(
        default=None, description="同じディメンションを複数の役割で使う場合の役割名（例: ship_to）"
    )
    attributes: list[Attribute] = Field(default_factory=list)
    hierarchy: list[Identifier] = Field(
        default_factory=list, description="粗い→細かいの順の属性名（例: region, prefecture）"
    )
    time_grain: TimeGrain | None = Field(default=None, description="When の最小粒度")
    business_key: SourceRef | None = Field(default=None, description="ソース上の自然キー")

    @property
    def ref(self) -> str:
        return self.role or self.name

    @model_validator(mode="after")
    def _check(self) -> Dimension:
        names = [a.name for a in self.attributes]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.name}: 属性名が重複しています")
        unknown = [h for h in self.hierarchy if h not in names]
        if unknown:
            raise ValueError(f"{self.name}: 階層に存在しない属性があります: {unknown}")
        return self


class EventExample(_Base):
    """インタビューで集めた具体例（event story）。unit test の期待値になる。"""

    values: dict[str, str | int | float | None] = Field(
        description="キーは `<dimension ref>.<attribute>` または measure 名"
    )


class BusinessEvent(_Base):
    """「誰が・何をする」のビジネスイベント。fct 1 つに対応する。"""

    name: Identifier = Field(description="例: customer_orders_product。fct_<name> の元になる")
    label: str = Field(description="例: 顧客が商品を注文する")
    subject: str = Field(description="Who（主語）")
    verb: str = Field(description="動詞")
    object: str = Field(description="What（目的語）")
    story_type: StoryType = StoryType.DISCRETE
    grain: str | None = Field(default=None, description="1 行が何を表すか（日本語）")
    grain_keys: list[str] = Field(
        default_factory=list, description="粒度を一意にするキー（dimension ref またはソースカラム）"
    )
    measures: list[Measure] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    examples: list[EventExample] = Field(default_factory=list)
    priority: Level | None = None
    impact: Level | None = None
    effort: Level | None = None
    source_tables: list[str] = Field(default_factory=list)

    @property
    def fact_type(self) -> FactType:
        return FACT_TYPE_BY_STORY[self.story_type]

    def dimension(self, ref: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.ref == ref), None)

    def measure(self, name: str) -> Measure | None:
        return next((m for m in self.measures if m.name == name), None)

    @model_validator(mode="after")
    def _unique(self) -> BusinessEvent:
        for kind, names in (
            ("measure", [m.name for m in self.measures]),
            ("dimension", [d.ref for d in self.dimensions]),
        ):
            if len(names) != len(set(names)):
                raise ValueError(f"{self.name}: {kind} が重複しています")
        return self


class MetricType(StrEnum):
    SIMPLE = "simple"
    RATIO = "ratio"
    DERIVED = "derived"
    CUMULATIVE = "cumulative"


class MetricStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    BLOCKED = "blocked"


class MetricRequest(_Base):
    """「How many ○○ by △△」の形にした指標の要求。Semantic Layer の metric になる。"""

    name: Identifier
    label: str
    question: str = Field(description="ユーザーの元の言葉")
    framed_question: str = Field(description="How many の形に言い換えた問い")
    type: MetricType = MetricType.SIMPLE
    event: Identifier
    measure: Identifier | None = Field(default=None, description="simple / cumulative のとき")
    numerator: Identifier | None = Field(default=None, description="ratio のとき（metric 名）")
    denominator: Identifier | None = Field(default=None, description="ratio のとき（metric 名）")
    expr: str | None = Field(default=None, description="derived のとき")
    group_by: list[str] = Field(
        default_factory=list, description="`<dimension ref>.<attribute>` の一覧"
    )
    status: MetricStatus = MetricStatus.DRAFT


class GapKind(StrEnum):
    DATA_UNAVAILABLE = "data_unavailable"
    UNANSWERED = "unanswered"
    CONFLICT = "conflict"


class Gap(_Base):
    """未解決事項。PR 本文に書かれ、data_unavailable は issue も起票される。"""

    kind: GapKind
    description: str
    event: Identifier | None = None
    w: W | None = None
    related: str | None = Field(default=None, description="関係する measure / 属性の参照")


class BeamModel(_Base):
    """BEAM 成果物のルート。beam/<name>.yml に保存される。"""

    schema_version: int = 1
    name: Identifier
    title: str
    request: str = Field(description="ユーザーの最初の要求文")
    events: list[BusinessEvent] = Field(default_factory=list)
    metrics: list[MetricRequest] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    finalized_at: datetime | None = None

    def event(self, name: str) -> BusinessEvent | None:
        return next((e for e in self.events if e.name == name), None)

    @model_validator(mode="after")
    def _unique(self) -> BeamModel:
        for kind, names in (
            ("event", [e.name for e in self.events]),
            ("metric", [m.name for m in self.metrics]),
        ):
            if len(names) != len(set(names)):
                raise ValueError(f"{kind} が重複しています")
        return self

    def finalize_issues(self) -> list[str]:
        """確定前に満たすべき条件のうち、満たしていないものを返す。空なら確定できる。"""
        from adp.beam.validation import finalize_issues

        return finalize_issues(self)

    def finalize(self) -> None:
        issues = self.finalize_issues()
        if issues:
            raise ValueError("確定できません: " + " / ".join(issues))
        self.finalized_at = datetime.now(UTC)
