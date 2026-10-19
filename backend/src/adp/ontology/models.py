"""オントロジーの型（#67）。正本は `ontology/ontology.yml`。

業務の概念（顧客・商品・注文…）と、その属性・関係・制約を表す。
源泉が増えたときは、まずここを更新してから fact / dim を整理する（#69）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_NAME = r"^[a-z][a-z0-9_]*$"
_TABLE = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
_COLUMN = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("label", "verb", "text", mode="before", check_fields=False)
    @classmethod
    def _one_line(cls, v: object) -> object:
        """表示の文字列は 1 行にする（改行で PR 本文や図に節を差し込ませない。#87 レビュー）。"""
        return " ".join(v.split()) if isinstance(v, str) else v


class Domain(_Strict):
    name: str = Field(pattern=_NAME)
    label: str = Field(min_length=1)


class Attribute(_Strict):
    name: str = Field(pattern=_NAME)
    label: str = Field(min_length=1)
    column: str = Field(pattern=_NAME)
    """概念の source の列。"""
    same_as: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    """別の概念の属性（`<概念>.<属性>`）と同じ業務の言葉なら、その属性（#137）。

    同じ値を別の源泉にも持つとき（注文の支払方法と、支払の支払方法など）に、どちらが正本かを示す。
    ラベルは正本と同じにする。同じラベルで same_as の無い属性が別々にあると、照合で落ちる。"""


class Concept(_Strict):
    name: str = Field(pattern=_NAME)
    label: str = Field(min_length=1)
    domain: str
    kind: Literal["entity", "event"]
    """entity（もの・人・場所。dim の候補）か event（出来事。fact の候補）。"""
    source: str = Field(pattern=_TABLE)
    key: str = Field(pattern=_NAME)
    model: str | None = Field(default=None, pattern=r"^(dim|fct)_[a-z0-9_]+$")
    """対応する整備済みモデル。entity → dim_*、event → fct_*。まだ無ければ None。"""
    attributes: list[Attribute] = Field(default_factory=list)

    @model_validator(mode="after")
    def _model_matches_kind(self) -> Concept:
        expected = "dim_" if self.kind == "entity" else "fct_"
        if self.model is not None and not self.model.startswith(expected):
            raise ValueError(f"概念 {self.name}（{self.kind}）のモデルは {expected}* にする")
        for what, values in (
            ("名前", [a.name for a in self.attributes]),
            ("列", [a.column for a in self.attributes]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"概念 {self.name} の属性の{what}が重複している")
        return self


class Relation(_Strict):
    from_: str = Field(alias="from", pattern=_NAME)
    to: str = Field(pattern=_NAME)
    verb: str = Field(min_length=1)
    """from → to の矢印のラベル（動詞）。"""
    via: str = Field(pattern=_COLUMN)
    """関係を表す列（<schema>.<table>.<column>）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Constraint(_Strict):
    concept: str
    text: str = Field(min_length=1)


class Ontology(_Strict):
    version: Literal[1] = 1
    domains: list[Domain]
    concepts: list[Concept]
    relations: list[Relation] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references(self) -> Ontology:
        """名前の重複と、存在しないドメイン・概念への参照を拒否する。"""
        for kind, names in (
            ("ドメイン", [d.name for d in self.domains]),
            ("概念", [c.name for c in self.concepts]),
        ):
            dup = {n for n in names if names.count(n) > 1}
            if dup:
                raise ValueError(f"{kind}の名前が重複している: {', '.join(sorted(dup))}")
        domains = {d.name for d in self.domains}
        concepts = {c.name for c in self.concepts}
        for c in self.concepts:
            if c.domain not in domains:
                raise ValueError(f"概念 {c.name} のドメイン {c.domain} が domains に無い")
        for r in self.relations:
            for end in (r.from_, r.to):
                if end not in concepts:
                    raise ValueError(f"関係「{r.verb}」の端 {end} が concepts に無い")
        for x in self.constraints:
            if x.concept not in concepts:
                raise ValueError(f"制約の概念 {x.concept} が concepts に無い")
        relations = [(r.from_, r.to, r.verb) for r in self.relations]
        if len(relations) != len(set(relations)):
            raise ValueError("同じ関係が重複している")
        constraints = [(x.concept, x.text) for x in self.constraints]
        if len(constraints) != len(set(constraints)):
            raise ValueError("同じ制約が重複している")
        models = [c.model for c in self.concepts if c.model]
        dup = {m for m in models if models.count(m) > 1}
        if dup:
            raise ValueError(f"1 つのモデルが複数の概念に対応している: {', '.join(sorted(dup))}")
        attrs = {f"{c.name}.{a.name}": a for c in self.concepts for a in c.attributes}
        for ref, a in attrs.items():
            if a.same_as is None:
                continue
            target = attrs.get(a.same_as)
            if target is None or a.same_as == ref:
                raise ValueError(f"属性 {ref} の same_as {a.same_as} が、ほかの概念の属性に無い")
            if target.same_as is not None:
                raise ValueError(
                    f"属性 {ref} の same_as {a.same_as} は正本ではない（same_as を持つ）"
                )
            if target.label != a.label:
                raise ValueError(
                    f"属性 {ref} のラベル「{a.label}」が、same_as の {a.same_as}"
                    f"「{target.label}」と違う"
                )
        return self

    def concept(self, name: str) -> Concept:
        return next(c for c in self.concepts if c.name == name)

    def attribute_at(self, table: str, column: str) -> tuple[Concept, Attribute] | None:
        """源泉の列（<schema>.<table> の column）を属性に持つ概念と、その属性。"""
        for c in self.concepts:
            if c.source == table:
                for a in c.attributes:
                    if a.column == column:
                        return c, a
        return None

    def expr_column(self, table: str, expr: str) -> str | None:
        """式（`coalesce(region, '地方なし')` など）が使う、オントロジーの属性の列。

        文字列のリテラルと関数名を除いた識別子のうち、その源泉の属性の列がちょうど 1 つのときだけ
        返す（複数の列から作る式は、どの言葉にそろえるか決まらないので見ない。#137）。"""
        known = {a.column for c in self.concepts if c.source == table for a in c.attributes}
        words = re.findall(r"\b([a-z_][a-z0-9_]*)\b(?!\s*\()", re.sub(r"'[^']*'", "", expr.lower()))
        used = known & set(words)
        return used.pop() if len(used) == 1 else None

    def duplicate_labels(self) -> list[str]:
        """同じラベルなのに same_as で結ばれていない、別々の属性（業務の言葉の二重定義。#137）。

        同じものなら same_as で正本を示し、違うものならラベルを分ける（居住地方・所在地方など）。"""
        groups: dict[str, set[str]] = {}
        for c in self.concepts:
            for a in c.attributes:
                groups.setdefault(a.label, set()).add(a.same_as or f"{c.name}.{a.name}")
        return [
            f"ラベル「{label}」の属性が別々に定義されている（{'、'.join(sorted(refs))}）。"
            "同じものなら片方に same_as で正本を書き、違うものならラベルを分ける"
            for label, refs in groups.items()
            if len(refs) > 1
        ]

    def word_mismatch(self, table: str, column: str, label: str) -> str | None:
        """同じ源泉の列の属性に付けたラベルが、オントロジー（業務の言葉の正本。#137）と違えば、
        その説明。BEAM 成果物の属性を確かめるのに使う。

        名前は見ない（名前は dim の列名と指標の切り口の参照名になり、出来事ごとの縮退ディメンション
        では「<ディメンション>.<属性>」の組で決まる。列名の決まりと合わせて決める。#137）。"""
        found = self.attribute_at(table, column)
        if found is None or found[1].label == label:
            return None
        concept, attr = found
        return (
            f"{table}.{column} は、オントロジーでは「{concept.label}」の属性 {attr.name}"
            f"「{attr.label}」です。ラベルを「{attr.label}」にしてください"
        )


def link_models(ontology: Ontology, models: list[tuple[str, str]]) -> tuple[Ontology, list[str]]:
    """(源泉, モデル) の組から、まだモデルの無い概念にモデルを書き込む（#69 の fact / dim の整理）。

    概念の種類と接頭辞が合うもの（entity ↔ dim_、event ↔ fct_）だけ。書き込んだ概念の名前も返す。
    """
    linked: list[str] = []
    concepts = []
    # 概念とモデルは 1 対 1（#114）。既にどこかの概念にあるモデル（再利用した既存の fct など）は、
    # 同じ源泉のほかの概念に書き込まない
    taken = {c.model for c in ontology.concepts if c.model}
    for c in ontology.concepts:
        prefix = "dim_" if c.kind == "entity" else "fct_"
        # 同じ源泉に fct と dim があっても、概念の種類に合うものを選ぶ
        model = next(
            (m for s, m in models if s == c.source and m.startswith(prefix) and m not in taken),
            None,
        )
        if c.model is None and model is not None:
            concepts.append(c.model_copy(update={"model": model}))
            linked.append(c.name)
            taken.add(model)
        else:
            concepts.append(c)
    if not linked:
        return ontology, []
    return Ontology.model_validate(
        {
            **ontology.model_dump(mode="json", by_alias=True, exclude_none=True),
            "concepts": [c.model_dump(mode="json", exclude_none=True) for c in concepts],
        }
    ), linked


def load(path: Path) -> Ontology:
    """読み込んで検証する。YAML の構文の誤りも ValueError にする。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"YAML として読めない: {e}") from e
    return Ontology.model_validate(data)


HEADER = """\
# オントロジーの正本（#67）。docs/ontology.md はここから生成する（make ontology）。
# 手で md を編集しない。取り込み（#69）と生成（作った fct / dim の書き込み）が
# この形で書き直すので、コメントはここ（adp.ontology.models.HEADER）に書く。
#
# - domains: 業務のまとまり（図の枠）
# - concepts: 概念。kind は entity（もの・人・場所）か event（出来事。fact の候補）
#   - source / key: 概念の元になる源泉のテーブルと、その識別子の列
#   - attributes: 属性。column は source の列。ラベルは業務の言葉の正本（BEAM・dim と照合）
#     - same_as: 同じ言葉を別の概念にも持つときの正本（<概念>.<属性>）。同じラベルは結ぶ
#   - model: 対応する整備済みモデル（entity → dim_*、event → fct_*）。まだ無ければ省略
# - relations: 概念どうしの関係。verb は from → to の矢印のラベル（動詞）
#   - via: 関係を表す列（<schema>.<table>.<column>）
# - constraints: 業務上の制約・注意（図の注記）
"""


def dump(ontology: Ontology) -> str:
    """正本の形（先頭の説明＋決まった並び）。書き直しても、追加・変更した行だけが差分になる。"""
    return HEADER + yaml.safe_dump(
        ontology.model_dump(mode="json", by_alias=True, exclude_none=True),
        allow_unicode=True,
        sort_keys=False,
        width=1000,
    )


def mermaid_id(name: str) -> str:
    """Mermaid のノード ID（英小文字・数字・_ のみ）。

    予約語（end など）と重ならないよう接頭辞を付ける。
    """
    return "c_" + re.sub(r"[^a-z0-9_]", "_", name)
