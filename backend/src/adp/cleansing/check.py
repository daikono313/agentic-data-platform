"""クレンジングの定義の検証と、源泉のプロファイルからの雛形づくり（#68）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from adp.catalog.catalog import Catalog, TableProfile
from adp.cleansing.models import CleansingSpec, ColumnRule, Dedupe, OrderBy, load_all, load_defaults
from adp.cleansing.render import GENERATED, Rendered, SourceColumn, output_columns, render

ACCEPTED_VALUES_MAX = 10
"""値の種類がこれ以下の文字列の列は、雛形で accepted_values を提案する。"""


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rendered: list[Rendered] = field(default_factory=list)


def source_columns(catalog: Catalog, source: str) -> list[SourceColumn]:
    return [SourceColumn(c.name, c.data_type) for c in catalog.columns(source)]


def validate(spec: CleansingSpec, columns: list[SourceColumn]) -> list[str]:
    """定義が源泉の列と矛盾しないか（存在しない列・出力に無いキーなど）。"""
    errors: list[str] = []
    names = {c.name for c in columns}
    for rule in spec.columns:
        if rule.name not in names:
            errors.append(f"{spec.source}: 列 {rule.name} が源泉に無い")
    for x in spec.exclude:
        if x.column not in names:
            errors.append(f"{spec.source}: exclude の列 {x.column} が源泉に無い")
    out = output_columns(spec, [c for c in columns])
    dup = {n for n in out if out.count(n) > 1}
    if dup:
        errors.append(f"{spec.source}: 出力の列名が重複している: {', '.join(sorted(dup))}")
    if spec.key is not None and spec.key not in out:
        errors.append(f"{spec.source}: key {spec.key} がクレンジング後の列に無い")
    if spec.dedupe is not None:
        for c in [*spec.dedupe.key, *(o.column for o in spec.dedupe.order_by)]:
            if c not in out:
                errors.append(f"{spec.source}: dedupe の列 {c} がクレンジング後の列に無い")
    return errors


def cast_failures(catalog: Catalog, spec: CleansingSpec) -> list[str]:
    """型変換で NULL になってしまう値の数（事前の確認。0 でなければ警告）。"""
    warnings: list[str] = []
    for rule in spec.columns:
        if rule.cast is None or rule.drop:
            continue
        failed = catalog.count_cast_failures(spec.source, rule.name, rule.cast)
        if failed:
            warnings.append(
                f"{spec.source}.{rule.name}: {rule.cast} に変換できない値が"
                f" {failed} 行（NULL になる）"
            )
    return warnings


def check_all(warehouse: Path, catalog: Catalog) -> CheckResult:
    """全定義を検証して生成物を作る。生成物が最新かどうかは呼び出し側で比べる。"""
    result = CheckResult()
    cleansing_dir = warehouse / "cleansing"
    defaults = load_defaults(cleansing_dir)
    sources = {t.qualified for t in catalog.list_sources()}
    specs = load_all(cleansing_dir)
    for spec in specs:
        if spec.source not in sources:
            result.errors.append(f"{spec.source}: 源泉がカタログに無い")
            continue
        columns = source_columns(catalog, spec.source)
        errors = validate(spec, columns)
        result.errors += errors
        if errors:
            continue
        result.warnings += cast_failures(catalog, spec)
        result.rendered.append(render(spec, defaults, columns))
    # 定義が無くなったのに残っている生成物
    expected = {r.sql_path for r in result.rendered} | {r.yml_path for r in result.rendered}
    models = {spec.model_name for spec in specs}
    for path in sorted((warehouse / "models" / "staging").glob("stg_*.*")):
        rel = f"models/staging/{path.name}"
        if rel in expected or path.stem in models:
            continue
        if GENERATED in path.read_text(encoding="utf-8").split("\n", 1)[0]:
            result.errors.append(f"{rel}: 定義（warehouse/cleansing/）が無い生成物が残っている")
    return result


def draft(profile: TableProfile) -> CleansingSpec:
    """源泉のプロファイルから、クレンジングの定義の雛形を作る（ユーザーが確認して直す前提）。

    - 主キー候補を key に。無ければ、最初の `_id` 列で重複を除く案（更新日時の新しい行を残す）
    - 値の種類が少ない文字列の列は accepted_values（今ある値）
    - 日時の文字列（`_at` / `_on` / `_date` で VARCHAR）は型変換
    """
    by_name = {p.column: p for p in profile.profiles}
    rules: list[ColumnRule] = []
    for c in profile.columns:
        p = by_name.get(c.name)
        text = c.data_type.upper().startswith("VARCHAR")
        kwargs: dict[str, object] = {"name": c.name}
        if text and c.name.endswith("_at"):
            kwargs["cast"] = "timestamp"
        elif text and c.name.endswith(("_on", "_date")):
            kwargs["cast"] = "date"
        elif (
            text
            and p is not None
            and 0 < p.distinct_count <= ACCEPTED_VALUES_MAX
            and len(p.top_values) >= p.distinct_count - (1 if p.null_count else 0)
            and not c.name.endswith(("_id", "_name"))
        ):
            values = sorted(v for v, _ in p.top_values if v is not None)
            if values and all("{" not in v and "}" not in v and "\\" not in v for v in values):
                kwargs["accepted_values"] = values
        if len(kwargs) > 1:
            rules.append(ColumnRule.model_validate(kwargs))
    # キーは `_id` の主キー候補を優先する（日時などがたまたま一意なだけの列を選ばない）
    candidates = profile.primary_key_candidates
    id_keys = [c for c in candidates if c.endswith("_id")]
    key = id_keys[0] if id_keys else None
    dedupe = None
    ids = [c.name for c in profile.columns if c.name.endswith("_id")]
    updated = [c.name for c in profile.columns if c.name in ("updated_at", "loaded_at")]
    if key is None and ids and updated:
        key = ids[0]
        dedupe = Dedupe(key=[ids[0]], order_by=[OrderBy(column=updated[0], desc=True)])
    elif key is None and candidates:
        key = candidates[0]
    # 説明は定義の文字列の検証（{ } \\ を拒否）に通るよう、その文字を除く
    description = profile.description.split("\n")[0].translate(str.maketrans("", "", "{}\\"))
    return CleansingSpec.model_validate(
        {
            "source": f"{profile.schema_name}.{profile.name}",
            "description": f"{description}（{profile.schema_name}.{profile.name} をクレンジング）"
            if description
            else f"{profile.schema_name}.{profile.name} をクレンジング",
            "key": key,
            "columns": [r.model_dump(by_alias=True, exclude_defaults=True) for r in rules],
            "dedupe": dedupe.model_dump(by_alias=True) if dedupe else None,
        }
    )
