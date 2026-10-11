"""クレンジングの定義（#68）。正本は `warehouse/cleansing/<schema>.<table>.yml` と `_defaults.yml`。

源泉 1 テーブルにつき 1 つの定義から、クレンジング層（staging の `stg_<table>`）の SQL とテストを
決定的に生成する（LLM を使わない）。定義は構造化された項目だけで、任意の SQL・Jinja は書けない。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENT = r"^[a-z_][a-z0-9_]*$"
TABLE = r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$"
CAST_TYPES = {
    "integer",
    "bigint",
    "double",
    "varchar",
    "date",
    "timestamp",
    "boolean",
}
_DECIMAL = re.compile(r"^decimal\((\d{1,2}),\s*(\d{1,2})\)$")
_TIMEZONE = re.compile(r"^(UTC|[A-Z][A-Za-z_]+/[A-Z][A-Za-z_]+)$")

Scalar = str | int | float | bool


def _check_literal(value: Scalar) -> Scalar:
    """SQL の文字列リテラルに入れる値。dbt の Jinja として解釈される文字と制御文字を拒否する。"""
    if isinstance(value, str) and (
        any(ch in value for ch in "{}\\") or any(ord(ch) < 0x20 for ch in value)
    ):
        raise ValueError(f"値に使えない文字（{{ }} \\ や制御文字）が含まれる: {value!r}")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Derive(_Strict):
    """列から新しい列を作る（例: 日時から日付を切り出す）。"""

    name: str = Field(pattern=IDENT)
    as_: Literal["date", "month", "year"] = Field(alias="as")
    description: str = ""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ColumnRule(_Strict):
    """列ごとのクレンジング。書かなかった項目は `_defaults.yml` の型ごとの既定に従う。"""

    name: str = Field(pattern=IDENT)
    """源泉の列名。"""
    rename: str | None = Field(default=None, pattern=IDENT)
    drop: bool = False
    cast: str | None = None
    """integer / bigint / double / decimal(p,s) / varchar / date / timestamp / boolean。
    変換できない値は NULL になる（try_cast）。"""
    trim: bool | None = None
    empty_to_null: bool | None = None
    case: Literal["lower", "upper"] | None = None
    values: dict[str, Scalar] = Field(default_factory=dict)
    """値の対応表（コードの正規化）。左の値を右の値に置き換える。"""
    timezone: tuple[str, str] | None = None
    """[変換元, 変換先]（例: [UTC, Asia/Tokyo]）。timestamp の列に使う。"""
    accepted_values: list[Scalar] = Field(default_factory=list)
    """クレンジング後に取りうる値（dbt の accepted_values テストになる）。"""
    not_null: bool = False
    description: str = ""
    derive: list[Derive] = Field(default_factory=list)

    @field_validator("cast")
    @classmethod
    def _cast_type(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in CAST_TYPES and not _DECIMAL.match(v):
            allowed = ", ".join(sorted(CAST_TYPES))
            raise ValueError(f"cast に使えない型: {v}（{allowed}, decimal(p,s)）")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: tuple[str, str] | None) -> tuple[str, str] | None:
        if v is not None and not all(_TIMEZONE.match(x) for x in v):
            raise ValueError(f"タイムゾーンの名前が正しくない: {v}")
        return v

    @field_validator("values")
    @classmethod
    def _values(cls, v: dict[str, Scalar]) -> dict[str, Scalar]:
        for k, x in v.items():
            _check_literal(k)
            _check_literal(x)
        return v

    @field_validator("accepted_values")
    @classmethod
    def _accepted(cls, v: list[Scalar]) -> list[Scalar]:
        for x in v:
            _check_literal(x)
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        _check_literal(v)
        return v

    @property
    def output(self) -> str:
        return self.rename or self.name


class Exclude(_Strict):
    """除外する行の条件（構造化された条件だけ。任意の SQL は書けない）。"""

    column: str = Field(pattern=IDENT)
    """源泉の列名（クレンジング前の値で判定する）。"""
    is_null: bool | None = None
    in_: list[Scalar] | None = Field(default=None, alias="in")
    equals: Scalar | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _one(self) -> Exclude:
        given = [x for x in (self.is_null, self.in_, self.equals) if x is not None]
        if len(given) != 1:
            raise ValueError("exclude は is_null / in / equals のどれか 1 つを書く")
        for x in [*(self.in_ or []), *([self.equals] if self.equals is not None else [])]:
            _check_literal(x)
        return self


class OrderBy(_Strict):
    column: str = Field(pattern=IDENT)
    desc: bool = False


class Dedupe(_Strict):
    """重複の除去。key ごとに order_by の先頭の 1 行を残す。"""

    key: list[str] = Field(min_length=1)
    order_by: list[OrderBy] = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def _key(cls, v: list[str]) -> list[str]:
        for k in v:
            if not re.match(IDENT, k):
                raise ValueError(f"不正な列名: {k}")
        return v


class CleansingSpec(_Strict):
    source: str = Field(pattern=TABLE)
    """`<schema>.<table>`（例: raw.orders）。"""
    model: str | None = Field(default=None, pattern=r"^stg_[a-z0-9_]+$")
    """生成するモデル名。省略時は stg_<table>。"""
    dbt_source: str | None = Field(default=None, pattern=IDENT)
    """dbt の source 名。省略時は schema（生成で別名の source として宣言した源泉で使う）。"""
    description: str = ""
    key: str | None = Field(default=None, pattern=IDENT)
    """クレンジング後の主キー（出力の列名）。unique / not_null のテストになる。"""
    columns: list[ColumnRule] = Field(default_factory=list)
    dedupe: Dedupe | None = None
    exclude: list[Exclude] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        _check_literal(v)
        return v

    @model_validator(mode="after")
    def _unique_columns(self) -> CleansingSpec:
        names = [c.name for c in self.columns]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"columns に同じ列が重複している: {', '.join(sorted(dup))}")
        return self

    @property
    def model_name(self) -> str:
        return self.model or "stg_" + self.source.rpartition(".")[2]

    @property
    def schema_name(self) -> str:
        return self.source.partition(".")[0]

    @property
    def table_name(self) -> str:
        return self.source.rpartition(".")[2]

    def rule(self, column: str) -> ColumnRule | None:
        return next((c for c in self.columns if c.name == column), None)


class TypeDefaults(_Strict):
    trim: bool = False
    empty_to_null: bool = False


class Defaults(_Strict):
    """型ごとの既定（`_defaults.yml`）。列の定義で上書きできる。"""

    text: TypeDefaults = TypeDefaults()
    """VARCHAR の列。"""


DEFAULTS_FILE = "_defaults.yml"


def spec_path(cleansing_dir: Path, source: str) -> Path:
    return cleansing_dir / f"{source}.yml"


def load_spec(path: Path) -> CleansingSpec:
    spec = CleansingSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if path.name != f"{spec.source}.yml":
        raise ValueError(f"{path.name}: ファイル名は <source>.yml（{spec.source}.yml）にする")
    return spec


def load_defaults(cleansing_dir: Path) -> Defaults:
    path = cleansing_dir / DEFAULTS_FILE
    if not path.exists():
        return Defaults()
    return Defaults.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def load_all(cleansing_dir: Path) -> list[CleansingSpec]:
    return [load_spec(p) for p in sorted(cleansing_dir.glob("*.yml")) if p.name != DEFAULTS_FILE]


def dump_spec(spec: CleansingSpec) -> str:
    return yaml.safe_dump(
        spec.model_dump(mode="json", by_alias=True, exclude_defaults=True),
        allow_unicode=True,
        sort_keys=False,
    )
