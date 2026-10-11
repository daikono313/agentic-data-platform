"""クレンジングの定義の検証と、源泉のプロファイルからの雛形づくり（#68）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

from adp.catalog.catalog import Catalog, TableProfile
from adp.cleansing.models import (
    DEFAULTS_FILE,
    IDENT,
    CleansingSpec,
    ColumnRule,
    Dedupe,
    OrderBy,
    load_defaults,
    load_spec,
)
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


def _kind(data_type: str) -> str:
    t = data_type.upper()
    if t.startswith(("VARCHAR", "TEXT", "STRING", "CHAR")):
        return "text"
    if t.startswith("TIMESTAMP"):
        return "timestamp"
    if t == "DATE":
        return "date"
    return "other"


def validate(spec: CleansingSpec, columns: list[SourceColumn]) -> list[str]:
    """定義が源泉の列と矛盾しないか（存在しない列・出力に無いキー・型に合わない処理など）。"""
    errors: list[str] = []
    types = {c.name: c.data_type for c in columns}
    names = set(types)
    for rule in spec.columns:
        if rule.name not in names:
            errors.append(f"{spec.source}: 列 {rule.name} が源泉に無い")
            continue
        # 型変換の後の型で判定する
        kind = _kind(rule.cast or types[rule.name])
        where = f"{spec.source}.{rule.name}"
        if kind != "text" and (rule.case or rule.trim or rule.empty_to_null):
            errors.append(f"{where}: trim / case / empty_to_null は文字列の列にだけ使える")
        if kind != "text" and rule.values:
            errors.append(f"{where}: values（値の対応表）は文字列の列にだけ使える")
        if kind != "timestamp" and rule.timezone:
            errors.append(f"{where}: timezone は timestamp の列にだけ使える（cast も使える）")
        if kind not in ("timestamp", "date") and rule.derive:
            errors.append(f"{where}: derive は日時・日付の列にだけ使える（cast も使える）")
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
    try:
        defaults = load_defaults(cleansing_dir)
    except (ValueError, yaml.YAMLError) as e:
        result.errors.append(f"cleansing/_defaults.yml: {e}")
        return result
    sources = {t.qualified for t in catalog.list_sources()}
    specs: list[CleansingSpec] = []
    for path in sorted(cleansing_dir.glob("*.y*ml")):
        if path.name == DEFAULTS_FILE:
            continue
        if path.suffix != ".yml":
            result.errors.append(f"cleansing/{path.name}: 拡張子は .yml にする")
            continue
        try:
            specs.append(load_spec(path))
        except (ValueError, yaml.YAMLError) as e:
            result.errors.append(f"cleansing/{path.name}: {e}")
    for spec in specs:
        if spec.source not in sources:
            result.errors.append(f"{spec.source}: 源泉がカタログに無い")
            continue
        columns = source_columns(catalog, spec.source)
        errors = validate(spec, columns)
        result.errors += errors
        if errors:
            continue
        try:
            result.warnings += cast_failures(catalog, spec)
            result.rendered.append(render(spec, defaults, columns))
        except (ValueError, duckdb.Error) as e:
            result.errors.append(f"{spec.source}: {e}")
    # 定義の無い stg（手で書いたもの、定義を消した後の残り）。stg は定義から生成する（ADR 0004）
    models = {spec.model_name for spec in specs}
    for path in sorted((warehouse / "models" / "staging").glob("stg_*.*")):
        if path.stem in models:
            continue
        rel = f"models/staging/{path.name}"
        generated = GENERATED in path.read_text(encoding="utf-8").split("\n", 1)[0]
        result.errors.append(
            f"{rel}: 定義（warehouse/cleansing/）が無い"
            + ("生成物が残っている" if generated else "（stg は定義から生成する）")
        )
    return result


def draft(profile: TableProfile) -> CleansingSpec:
    """源泉のプロファイルから、クレンジングの定義の雛形を作る（ユーザーが確認して直す前提）。

    - 主キー候補を key に。無ければ、最初の `_id` 列で重複を除く案（更新日時の新しい行を残す）
    - 値の種類が少ない文字列の列は accepted_values（今ある値）
    - 日時の文字列（`_at` / `_on` / `_date` で VARCHAR）は型変換
    """
    by_name = {p.column: p for p in profile.profiles}
    rules: list[ColumnRule] = []
    # 定義に書けない列名（大文字など）は雛形に入れない（ユーザーが直す）
    columns = [c for c in profile.columns if re.fullmatch(IDENT, c.name)]
    for c in columns:
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
            if values:
                kwargs["accepted_values"] = values
        if len(kwargs) > 1:
            try:
                rules.append(ColumnRule.model_validate(kwargs))
            except ValueError:  # 値に使えない文字（制御文字など）があれば、その列は雛形に入れない
                continue
    # キーは `_id` の主キー候補を優先する（日時などがたまたま一意なだけの列を選ばない）
    candidates = [k for k in profile.primary_key_candidates if re.fullmatch(IDENT, k)]
    id_keys = [c for c in candidates if c.endswith("_id")]
    key = id_keys[0] if id_keys else None
    dedupe = None
    ids = [c.name for c in columns if c.name.endswith("_id")]
    updated = [c.name for c in columns if c.name in ("updated_at", "loaded_at")]
    if key is None and ids and updated:
        key = ids[0]
        dedupe = Dedupe(key=[ids[0]], order_by=[OrderBy(column=updated[0], desc=True)])
    elif key is None and candidates:
        key = candidates[0]
    # 説明は定義の文字列の検証（{ } \\ を拒否）に通るよう、その文字を除く
    description = "".join(
        ch for ch in profile.description.split("\n")[0] if ch not in "{}\\" and ord(ch) >= 0x20
    )
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
