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
    ExistingFact,
    FactTable,
    IssueSeverity,
    Join,
    PhysicalDesign,
    SourceTable,
)
from adp.ontology.models import Concept, Ontology, link_models


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


def _grain_refs(event: BusinessEvent) -> list[SourceRef]:
    refs = []
    for key in event.grain_keys:
        if (dg := event.degenerate(key)) is not None:
            refs.append(dg.source)
        elif (d := event.dimension(key)) is not None and d.fact_key is not None:
            refs.append(d.fact_key)
    return refs


def base_table(event: BusinessEvent, schema: SchemaInfo) -> str | None:
    """fct の 1 行の元になるテーブル: 粒度のキーのテーブルのうち、他のすべてへ多対一で結合できる
    もの（最も細かいテーブル）。決まらなければ None。"""
    tables = list(dict.fromkeys(r.table for r in _grain_refs(event)))
    for candidate in tables:
        src = _Sources(candidate, schema)
        if all(src.reachable(t) for t in tables):
            return candidate
    return None


def grain_sources(event: BusinessEvent) -> frozenset[str]:
    """粒度のキーの源泉（raw.order_items.order_item_id など）。名前が違っても粒度を比べられる。"""
    return frozenset(f"{r.table}.{r.column or r.expr}" for r in _grain_refs(event))


class DesignBuilder:
    def __init__(
        self,
        schema: SchemaInfo,
        existing_dims: dict[str, set[str]] | None = None,
        ontology: Ontology | None = None,
        existing_facts: dict[str, ExistingFact] | None = None,
    ):
        self.schema = schema
        self.existing_dims = existing_dims or {}
        self.ontology = ontology
        """あれば、ディメンションの名前がオントロジーの概念の dim と合うかを確かめる（#69）。"""
        self.existing_facts = existing_facts
        """既存の fct の種類と粒度（確定した BEAM 成果物から。用途の重複の判定に使う。#113）。
        None なら比べない（オントロジーの名前だけで判定する）。"""
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
        self._check_ontology_words(beam)
        self._check_ontology_sources(facts, dims)
        self._check_duplicate_facts(beam, facts)
        self._check_linkable(facts, dims)
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
            natural = next((c.name for c in dim.columns if c.role == ColumnRole.NATURAL_KEY), None)
            # 同じキーで識別するもの（同じ概念）。別のキーの dim（地方など）は _check_linkable で
            # 止める
            if concept is not None and natural == concept.key and concept.model != dim.name:
                name = (concept.model or "").removeprefix("dim_")
                self._blocker(
                    f"{dim.name} の源泉 {dim.base.table} は、オントロジーでは"
                    f"概念「{concept.label}」（{concept.model}）です。"
                    f"ディメンションの名前を {name} にして、{concept.model} を再利用してください",
                    dim.dimension,
                )

    def _check_ontology_words(self, beam: BeamModel) -> None:
        """属性のラベルが、オントロジーの属性（業務の言葉の正本）と同じか（#137）。

        - 同じ源泉の列の属性（`ontology.check.beam_mismatches` と同じ。式は値を置き換えるだけの
          ものだけ）
        - 概念の dim に作る列（属性の名前）が概念の属性の列なら、その属性。dim の yml の説明は
          属性のラベルになり、`make ontology` が dim の説明を照合する

        違うまま確定すると、生成した PR が `make ontology` で落ちるので、確定の前に止める。
        """
        if self.ontology is None:
            return
        by_model = {c.model: c for c in self.ontology.concepts if c.kind == "entity" and c.model}
        seen: set[tuple[str, str]] = set()  # 出来事をまたいで共通のディメンションは 1 回だけ
        for event in beam.events:
            for dim in event.dimensions:
                concept = by_model.get(f"dim_{dim.name}")
                for a in dim.attributes:
                    if (dim.name, a.name) in seen:
                        continue
                    seen.add((dim.name, a.name))
                    mismatch = None
                    if a.source is not None:
                        column = a.source.column or self.ontology.expr_column(
                            a.source.table, a.source.expr or ""
                        )
                        if column is not None:
                            mismatch = self.ontology.word_mismatch(a.source.table, column, a.label)
                    if mismatch is None and concept is not None:
                        mismatch = self.ontology.word_mismatch(concept.source, a.name, a.label)
                        if mismatch:
                            mismatch += (
                                f"（dim_{dim.name} の列 {a.name} になるため。別の言葉なら、"
                                "属性の名前を変えてください）"
                            )
                    if mismatch:
                        self._blocker(
                            f"属性 {dim.name}.{a.name}: {mismatch}", f"{dim.name}.{a.name}"
                        )

    def _check_linkable(self, facts: list[FactTable], dims: list[DimTable]) -> None:
        """新しく作る fct / dim が、オントロジーのどれかの概念に結び付くか（#114）。

        概念とモデルは 1 対 1 で、結び付かないモデルは `make dbt-build` の検査で落ちる。生成の後に
        書き込むのと同じ規則（`link_models`。同じ源泉・種類が合う・概念にまだモデルが無い）で確かめ、
        生成した PR が自分の CI で落ちないよう、確定の前に止める。ほかの検査が止めたもの（未整理の
        源泉・同じ出来事の 2 つ目の fact・概念の dim と違う名前）は重ねて出さない。
        """
        if self.ontology is None:
            return
        existing = {c.model for c in self.ontology.concepts if c.model}
        known = {c.source for c in self.ontology.concepts}
        blocked = {i.ref for i in self.issues if i.severity == IssueSeverity.BLOCKER}
        models = [(f.base.table, f.name, f.event, "出来事") for f in facts]
        models += [(d.base.table, d.name, d.dimension, "もの・人・場所") for d in dims]
        new = [m for m in models if m[1] not in existing and m[0] in known and m[2] not in blocked]
        linked, _ = link_models(self.ontology, [(source, name) for source, name, _, _ in new])
        mapped = {c.model for c in linked.concepts if c.model}
        for source, name, ref, kind in new:
            if name in mapped:
                continue
            self._blocker(
                f"{name} を結び付けられるオントロジーの概念がありません（源泉 {source} に、"
                f"モデルの無い{kind}の概念が無い）。概念とモデルは 1 対 1 なので、既存のモデルを"
                "使うか、オントロジーにこのモデル用の概念を足してから作ってください",
                ref,
            )

    def _check_ontology_sources(self, facts: list[FactTable], dims: list[DimTable]) -> None:
        """オントロジーに無い（未整理の）源泉からモデルを作ろうとしていないか（#109）。

        源泉が増えたら、まずオントロジーに追加し（取り込み）、クレンジング層を作ってから fact / dim
        を整理する（CLAUDE.md の規約）。生成 PR がそれを迂回しないよう、確定の前に止める。
        ベースのテーブルだけでなく、結合するテーブルも見る（生成はそのクレンジング層も作る）。
        """
        if self.ontology is None:
            return
        known = {c.source for c in self.ontology.concepts}
        models: list[tuple[str, SourceTable, list[Join], str]] = [
            (f.name, f.base, f.joins, f.event) for f in facts
        ]
        models += [(d.name, d.base, d.joins, d.dimension) for d in dims]
        for model, base, joins, ref in models:
            tables = dict.fromkeys([base.table, *(j.right.table for j in joins)])
            for table in tables:
                if table in known:
                    continue
                role = "源泉" if table == base.table else "結合する源泉"
                self._blocker(
                    f"{model} の{role} {table} は、まだオントロジーにありません（未整理）。"
                    "開始画面の源泉一覧の「取り込む」から先に取り込んでください。"
                    "取り込みの PR がマージされ、手元に反映（git pull など）されると確定できます",
                    ref,
                )

    def _check_duplicate_facts(self, beam: BeamModel, facts: list[FactTable]) -> None:
        """同じ出来事の fact がすでにあるのに、用途の重なる fact を作ろうとしていないか（#113）。

        オントロジーの概念とモデルは 1 対 1（#114）。出来事（event）の概念にモデル（fct）があれば、
        その源泉の fact はそれだけにする（用途が同じ fact を 2 つ作らない）。
        - 種類（transaction など）と粒度が同じなら、イベントの table_name を合わせて既存の fact に
          指標を足す（名前が同じなら、そのまま足す）
        - 種類か粒度が違う fact は、先にその fact 用の概念をオントロジーに足す。同じ源泉にモデルの
          無い出来事の概念があれば、それに結び付けて作る。同じ名前のまま種類・粒度は変えられない
        - 既存の fct の種類・粒度が確定した BEAM 成果物から分からない（無い・食い違う）ときは、
          確かめるよう課題に出す
        同じ BEAM の中で、同じ源泉から 2 つの fact を作ることも止める。
        """
        by_base: dict[str, list[FactTable]] = {}
        for fact in facts:
            by_base.setdefault(fact.base.table, []).append(fact)
        for table, same_base in by_base.items():
            if len(same_base) > 1:
                names = "、".join(f"{f.name}（{f.event}）" for f in same_base)
                self._blocker(
                    f"同じ源泉 {table} から、複数の fact（{names}）を作ろうとしています。"
                    "用途が同じ fact を作らないよう、1 つの出来事にまとめてください",
                    same_base[1].event,
                )
        if self.ontology is None:
            return
        events: dict[str, list[Concept]] = {}
        for c in self.ontology.concepts:
            if c.kind == "event":
                events.setdefault(c.source, []).append(c)
        for fact in facts:
            concepts = events.get(fact.base.table, [])
            linked = [c for c in concepts if c.model]
            event = beam.event(fact.event)
            if not linked or event is None:
                continue
            same_name = next((c for c in linked if c.model == fact.name), None)
            concept = same_name or linked[0]
            model = concept.model or ""
            table_name = model.removeprefix("fct_")
            head = (
                f"{fact.name} の源泉 {fact.base.table} の出来事は、既に"
                f"「{concept.label}」（{model}）としてあります。"
            )
            if self.existing_facts is None:
                if same_name is None:
                    self._blocker(
                        head + f"用途が同じ fact を作らず、イベントの table_name を {table_name}"
                        f" にして {model} に指標を足してください"
                        "（measure と切り口は、list_facts にある既存の列名を使う）",
                        fact.event,
                    )
                continue
            known = self.existing_facts.get(model)
            if known is None or known.fact_type is None:
                reason = (
                    known.conflict
                    if known is not None and known.conflict
                    else "確定した BEAM 成果物（beam/）にありません"
                )
                if same_name is not None:
                    self._issue(
                        IssueSeverity.WARNING,
                        f"既存の {model} の種類・粒度を確かめられません（{reason}）。"
                        f"この要求の fact（{fact.fact_type.value}、粒度: {fact.grain}）と"
                        "同じかを、PR で確かめてください",
                        fact.event,
                    )
                else:
                    self._blocker(
                        head + f"ただし既存の {model} の種類・粒度を確かめられません"
                        f"（{reason}）。同じ種類・粒度なら、イベントの table_name を "
                        f"{table_name} にして {model} に指標を足してください。違うなら、先に"
                        "その fact 用の概念をオントロジーに追加してください（オーナーの確認が"
                        "必要です）",
                        fact.event,
                    )
                continue
            differs = []
            if known.fact_type != fact.fact_type:
                differs.append(f"種類（{known.fact_type.value} と {fact.fact_type.value}）")
            new_grain = grain_sources(event)
            if known.grain_sources and new_grain and known.grain_sources != new_grain:
                differs.append(
                    f"粒度のキー（{', '.join(sorted(known.grain_sources))} と "
                    f"{', '.join(sorted(new_grain))}）"
                )
            if differs:
                if same_name is None and any(not c.model for c in concepts):
                    continue  # 種類・粒度の違う fact 用の概念が足してある（それに結び付ける）
                what = "・".join(differs)
                if same_name is not None:
                    message = (
                        f"既存の {model} と、この要求の fact の{what}が違います。"
                        "既存の fct の種類・粒度は変えられません。"
                    )
                else:
                    message = head + f"{what}が違います。"
                self._blocker(
                    message + "オントロジーの概念とモデルは 1 対 1 なので、種類・粒度の違う"
                    " fact を作るには、別の名前にして、先にその fact 用の概念をオントロジーに"
                    "追加してください（オーナーの確認が必要です）",
                    fact.event,
                )
                continue
            if same_name is None:
                self._blocker(
                    head + f"用途が同じ fact を作らず、イベントの table_name を {table_name}"
                    f" にして {model} に指標を足してください"
                    "（measure と切り口は、list_facts にある既存の列名を使う）",
                    fact.event,
                )

    # --- fct -------------------------------------------------------------------------------

    def _fact(self, event: BusinessEvent) -> FactTable | None:
        p = f"[{event.name}]"
        if event.fact_type == FactType.PERIODIC_SNAPSHOT:
            self._blocker(f"{p} 定期スナップショット（recurring）は MVP では生成できません")
            return None
        base = base_table(event, self.schema)
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
            elif d.masterless:
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
                    grain_columns.append(gd.ref if gd.masterless else f"{gd.ref}_key")
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
                if d.w != W.WHEN and not d.masterless:
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
                elif d.masterless:
                    if fact.column(d.ref) is None:
                        self._blocker(f"{p} 切り口 {ref} の列が {fact.name} にありません", ref)
                elif (dim := dims.get(d.name)) is None or all(c.name != attr for c in dim.columns):
                    self._blocker(f"{p} 切り口 {ref} の列が dim_{d.name} にありません", ref)


def _time_base(d: Dimension) -> str:
    """When の列名の基になる名前（ordered_at → ordered）。"""
    if d.fact_key is not None and d.fact_key.column and d.fact_key.column.endswith("_at"):
        return d.fact_key.column.removesuffix("_at")
    return d.ref
