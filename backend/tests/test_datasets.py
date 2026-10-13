"""データ一覧（#38）を、生成済みの指標を含む warehouse の複製で確かめる。"""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from adp.api.app import create_app
from adp.api.service import SessionService
from adp.api.store import Store
from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.datasets import DatasetIndex
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.project import ProjectInfo
from adp.interview import Interviewer
from adp.llm import LLMSettings, ScriptedRunner
from tests.generation.test_dbt_codegen import DBT, FIXTURES, _warehouse_copy


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("repo")
    warehouse = _warehouse_copy(root)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    info.write(generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam).files)
    (root / "beam").mkdir()
    (root / "beam" / "sales_by_store.yml").write_text(
        yaml.safe_dump(beam.model_dump(mode="json"), allow_unicode=True), encoding="utf-8"
    )
    env = os.environ | {"DBT_PROFILES_DIR": str(warehouse)}
    for args in (["deps"], ["parse"]):
        subprocess.run([str(DBT), *args], cwd=warehouse, env=env, check=True, capture_output=True)
    return root


def test_datasets_lists_metrics_models_sources_beams(repo: Path) -> None:
    catalog = Catalog(repo / "warehouse/data/dev.duckdb", repo / "warehouse/target/manifest.json")
    index = DatasetIndex(repo, catalog)
    data = index.all()
    assert data.errors == []
    revenue = next(m for m in data.metrics if m.name == "revenue")
    assert revenue.label == "売上金額" and revenue.model == "fct_order_lines"
    assert revenue.beam == "sales_by_store"
    names = {d.name: d.label for d in revenue.dimensions}
    kinds = {d.name: d.kind for d in revenue.dimensions}
    assert kinds["metric_time"] == "time" and kinds["store__channel"] == "categorical"
    assert next(d for d in revenue.dimensions if d.name == "store__channel").entity == "store"
    assert "metric_time" in names and "store__channel" in names
    assert names["store__channel"] != "store__channel"  # 日本語の表示名が付く
    assert {"fct_order_lines", "dim_customer", "dim_store"} <= {m.name for m in data.models}
    assert data.models[0].kind == "fct"
    orders = next(s for s in data.sources if s.name == "raw.orders")
    assert orders.row_count > 0 and orders.description
    # 活用状況（#72）: 使っている fact / dim。使われていない源泉は空
    assert orders.used_by == ["fct_order_lines"]
    assert next(s for s in data.sources if s.name == "raw.payments").used_by == []
    assert (
        {c.name: c.data_type for c in orders.columns}["ordered_at"].upper().startswith("TIMESTAMP")
    )
    assert [b.name for b in data.beams] == ["sales_by_store"]


def test_dimensions_without_mf_process_or_db_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切り口はプロセス内の MetricFlow から取り、mf を起動せず DuckDB もロックしない（#43）。"""
    import duckdb

    calls = []
    real = subprocess.run

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    index = DatasetIndex(repo, None)
    revenue = next(m for m in index.all().metrics if m.name == "revenue")
    assert "store__store_name" in [d.name for d in revenue.dimensions]
    assert calls == []
    # dbt build などが書き込めること
    duckdb.connect(str(repo / "warehouse/data/dev.duckdb")).close()


def test_missing_build_is_reported(tmp_path: Path) -> None:
    (tmp_path / "warehouse").mkdir()
    index = DatasetIndex(tmp_path, Catalog(tmp_path / "x.duckdb"))
    data = index.all()
    assert data.metrics == [] and data.models == [] and data.sources == []
    assert any("make dbt-build" in e for e in data.errors)


def test_api_endpoint(repo: Path) -> None:
    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(
        Store(repo / ".adp/app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    index = DatasetIndex(repo, Catalog(repo / "warehouse/data/dev.duckdb"))
    client = TestClient(create_app(service, index))
    body = client.get("/datasets").json()
    assert {"metrics", "models", "sources", "beams", "errors"} <= set(body)
    assert TestClient(create_app(service)).get("/datasets").status_code == 503


def test_primary_entity_at_model_level_gets_labels() -> None:
    index = DatasetIndex(Path("."), None)
    manifest = {
        "semantic_models": [
            {
                "name": "fct_x",
                "primary_entity": "order_line",
                "entities": [],
                "dimensions": [
                    {"name": "order_status", "type": "categorical", "description": "注文ステータス"}
                ],
            }
        ]
    }
    assert index._dimension_labels(manifest)["order_line__order_status"][0] == "注文ステータス"


def test_broken_manifest_is_reported(tmp_path: Path) -> None:
    (tmp_path / "warehouse" / "target").mkdir(parents=True)
    (tmp_path / "warehouse" / "target" / "semantic_manifest.json").write_text("{broken")
    (tmp_path / "warehouse" / "target" / "manifest.json").write_text("{broken")
    data = DatasetIndex(tmp_path, Catalog(tmp_path / "x.duckdb")).all()
    assert data.metrics == [] and data.models == [] and data.errors


def test_source_profile(repo: Path) -> None:
    """源泉のプロファイル: 全列の統計・キーの候補・使っている整備済みモデル（#62）。"""
    catalog = Catalog(repo / "warehouse/data/dev.duckdb", repo / "warehouse/target/manifest.json")
    p = DatasetIndex(repo, catalog).profile("raw.order_items")
    assert p.row_count > 0 and p.description
    assert [c.column for c in p.profiles] == [c.name for c in p.columns]
    assert p.primary_key_candidates == ["order_item_id"]
    assert {f.column: f.references for f in p.foreign_key_candidates}["order_id"] == (
        "raw.orders.order_id"
    )
    assert "fct_order_lines" in p.used_by and all(m.startswith(("dim_", "fct_")) for m in p.used_by)
    product = next(c for c in p.profiles if c.column == "product_id")
    assert 0 < len(product.top_values) <= 5 and not product.is_unique


def test_source_profile_api(repo: Path) -> None:
    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(
        Store(repo / ".adp/app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    index = DatasetIndex(repo, Catalog(repo / "warehouse/data/dev.duckdb"))
    client = TestClient(create_app(service, index))
    body = client.get("/sources/raw.returns/profile").json()
    assert body["name"] == "returns" and body["profiles"]
    # 対象スキーマ外・識別子でない名前・存在しないテーブルは 404
    for bad in ("main.fct_order_lines", "raw.x;drop", "raw.nope"):
        assert client.get(f"/sources/{bad}/profile").status_code == 404, bad
    assert TestClient(create_app(service)).get("/sources/raw.returns/profile").status_code == 503


def test_source_profile_db_error_is_fixed_text(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DuckDB の例外は 500 にせず、本文を出さない定型文の 503 にする（guardrails §3）。"""
    import duckdb

    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    def broken(self, table, top=5, detail=False):  # type: ignore[no-untyped-def]
        raise duckdb.BinderException("secret detail")

    monkeypatch.setattr(Catalog, "profile_table", broken)
    service = SessionService(
        Store(repo / ".adp/app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    index = DatasetIndex(repo, Catalog(repo / "warehouse/data/dev.duckdb"))
    res = TestClient(create_app(service, index)).get("/sources/raw.returns/profile")
    assert res.status_code == 503 and "secret" not in res.text


def test_bus_matrix(repo: Path) -> None:
    """fact × dimension のバスマトリクスを entity の結合関係から作る（#62）。"""
    bus = DatasetIndex(repo, None).all().bus
    groups = {g.name: g for g in bus.groups}
    assert groups["dim_store"].kind == "dim" and groups["dim_store"].label
    assert "store__channel" in [a.name for a in groups["dim_store"].attributes]
    assert [g.kind for g in bus.groups][-1] == "degenerate"  # fact 自身の属性は最後
    row = next(r for r in bus.rows if r.name == "fct_order_lines")
    assert {"dim_store", "dim_customer", "dim_date"} <= set(row.groups)
    assert all(g in groups for g in row.groups)


def test_bus_matrix_from_manifest() -> None:
    index = DatasetIndex(Path("."), None)
    manifest = {
        "semantic_models": [
            {
                "name": "dim_a",
                "description": "Aディメンション（Who）。",
                "entities": [{"name": "a", "type": "primary"}],
                "dimensions": [{"name": "x", "type": "categorical", "description": "属性X（注）"}],
            },
            {
                "name": "dim_b",
                "entities": [{"name": "b", "type": "primary"}],
                "dimensions": [{"name": "y", "type": "categorical", "label": "Y"}],
            },
            {
                "name": "fct_f",
                "description": "出来事",
                "entities": [{"name": "f", "type": "primary"}, {"name": "a", "type": "foreign"}],
                "dimensions": [],
            },
        ]
    }
    index._semantic_manifest = lambda: manifest  # type: ignore[method-assign]
    bus = index.bus_matrix()
    assert [(g.name, g.label) for g in bus.groups] == [("dim_a", "A"), ("dim_b", "dim_b")]
    assert bus.groups[0].attributes[0].label == "属性X"
    assert [(r.name, r.label, r.groups) for r in bus.rows] == [("fct_f", "出来事", ["dim_a"])]


def test_bus_matrix_join_rules() -> None:
    """unique entity・1 段の snowflake・同じ entity の dim 2 つ・primary の無い fct（#62）。"""
    index = DatasetIndex(Path("."), None)

    def dim(name: str, *ents: tuple[str, str]) -> dict[str, Any]:
        return {
            "name": name,
            "entities": [{"name": n, "type": t} for n, t in ents],
            "dimensions": [{"name": "attr", "type": "categorical"}],
        }

    manifest = {
        "semantic_models": [
            dim("dim_product", ("product", "primary"), ("category", "foreign")),
            dim("dim_category", ("category", "primary")),
            dim("dim_customer", ("customer", "primary")),
            dim("dim_customer_scd", ("customer", "primary")),
            dim("dim_sku", ("sku_row", "primary"), ("sku", "unique")),
            dim("dim_unrelated", ("x", "primary")),
            {
                "name": "fct_sales",
                "entities": [
                    {"name": "sale", "type": "primary"},
                    {"name": "product", "type": "foreign"},
                    {"name": "customer", "type": "foreign"},
                    {"name": "sku", "type": "foreign"},
                ],
                "dimensions": [{"name": "status", "type": "categorical"}],
            },
            {
                "name": "fct_noprimary",
                "entities": [{"name": "customer", "type": "foreign"}],
                "dimensions": [{"name": "status", "type": "categorical"}],
            },
            {"description": "name の無いモデルは無視する"},
        ]
    }
    bus = index.bus_matrix(manifest)
    rows = {r.name: r.groups for r in bus.rows}
    assert set(rows["fct_sales"]) == {
        "dim_product",
        "dim_category",
        "dim_customer",
        "dim_customer_scd",
        "dim_sku",
        "fct_sales",
    }
    assert rows["fct_noprimary"] == ["dim_customer", "dim_customer_scd"]
    assert [g.name for g in bus.groups if g.kind == "degenerate"] == ["fct_sales"]


def test_broken_bus_matrix_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    index = DatasetIndex(Path("."), None)

    def broken(manifest=None):  # type: ignore[no-untyped-def]
        raise KeyError("name")

    monkeypatch.setattr(index, "bus_matrix", broken)
    errors: list[str] = []
    assert index._bus(errors).rows == [] and errors


def test_ontology_api(repo: Path, tmp_path: Path) -> None:
    """GET /ontology: 図と食い違いを返す。yml が無ければ 404、形式の誤りは 422（#67）。"""
    import shutil

    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(
        Store(tmp_path / "app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    catalog = Catalog(repo / "warehouse/data/dev.duckdb", repo / "warehouse/target/manifest.json")
    client = TestClient(create_app(service, DatasetIndex(repo, catalog)))
    if (repo / "ontology").exists():
        shutil.rmtree(repo / "ontology")
    assert client.get("/ontology").status_code == 404
    (repo / "ontology").mkdir()
    from tests.ontology_before_inventory import ontology_yaml

    # この repo には fct_stock_movements が無いので、在庫（raw.inventory）を取り込む前の状態にする
    (repo / "ontology" / "ontology.yml").write_text(ontology_yaml())
    body = client.get("/ontology").json()
    assert body["mermaid"].startswith("flowchart")
    # 後から追加された源泉の例（オントロジーに未整理）: 在庫（#69）と商品レビュー（S5）
    assert body["errors"] == [] and body["unmapped_sources"] == ["raw.inventory", "raw.reviews"]
    (repo / "ontology" / "ontology.yml").write_text("version: 1\ndomains: []\nconcepts: [1]\n")
    assert client.get("/ontology").status_code == 422
    shutil.rmtree(repo / "ontology")


def test_source_cleansing_api(repo: Path, tmp_path: Path) -> None:
    """GET /sources/{table}/cleansing: 今の定義と、プロファイルからの雛形（#68）。"""

    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(
        Store(tmp_path / "app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    catalog = Catalog(repo / "warehouse/data/dev.duckdb", repo / "warehouse/target/manifest.json")
    client = TestClient(create_app(service, DatasetIndex(repo, catalog)))
    body = client.get("/sources/raw.orders/cleansing").json()
    assert body["path"] == "warehouse/cleansing/raw.orders.yml"
    assert body["existing"] and "ordered_on" in body["existing"]
    assert "key: order_id" in body["draft"]
    body = client.get("/sources/raw.returns/cleansing").json()
    assert body["existing"] is None and "source: raw.returns" in body["draft"]
    assert client.get("/sources/main.x/cleansing").status_code == 404


def test_used_by_follows_intermediate_models_and_skips_tests() -> None:
    """源泉 → stg → int → fct のように何段でも辿る。テストは数えず、mart の無い stg は空（#72）。"""
    manifest = {
        "sources": {
            "source.p.raw.orders": {"schema": "raw", "name": "orders"},
            "source.p.raw.x": {"schema": "raw", "name": "x_alias", "identifier": "x"},
            "source.p.raw.lonely": {"schema": "raw", "name": "lonely"},
        },
        "nodes": {
            "model.p.stg_orders": {"name": "stg_orders", "resource_type": "model"},
            "model.p.int_orders": {"name": "int_orders", "resource_type": "model"},
            "model.p.fct_sales.v1": {"name": "fct_sales", "resource_type": "model"},
            "model.p.stg_x": {"name": "stg_x", "resource_type": "model"},
            "model.p.dim_x": {"name": "dim_x", "resource_type": "model"},
            "model.p.stg_lonely": {"name": "stg_lonely", "resource_type": "model"},
        },
        "child_map": {
            "source.p.raw.orders": ["model.p.stg_orders", "test.p.not_null_orders"],
            "model.p.stg_orders": ["model.p.int_orders"],
            "model.p.int_orders": ["model.p.fct_sales.v1"],
            "source.p.raw.x": ["model.p.stg_x"],
            "model.p.stg_x": ["model.p.dim_x"],
            "source.p.raw.lonely": ["model.p.stg_lonely"],
        },
        "parent_map": {
            "model.p.fct_sales.v1": ["model.p.int_orders"],
            "model.p.int_orders": ["model.p.stg_orders"],
            "model.p.stg_orders": ["source.p.raw.orders"],
            "model.p.dim_x": ["model.p.stg_x"],
            "model.p.stg_x": ["source.p.raw.x"],
        },
    }
    from adp.datasets import model_sources

    index = DatasetIndex(Path("."), None)
    assert index._used_by("raw", "orders", manifest) == ["fct_sales"]
    assert index._used_by("raw", "x", manifest) == ["dim_x"]
    assert index._used_by("raw", "lonely", manifest) == []
    assert model_sources(manifest)["fct_sales"] == ["raw.orders"]
    assert model_sources(manifest)["dim_x"] == ["raw.x"]


def test_used_by_is_unknown_without_manifest(tmp_path: Path) -> None:
    import duckdb

    db = tmp_path / "x.duckdb"
    duckdb.connect(str(db)).execute("create schema raw; create table raw.t (id int)").close()
    (tmp_path / "warehouse").mkdir()
    [source] = DatasetIndex(tmp_path, Catalog(db)).sources([])
    assert source.used_by is None  # 分からない（未活用とは言わない）


def test_beams_skip_broken_files_and_report_them(tmp_path: Path) -> None:
    """壊れた BEAM の YAML は一覧から外し、エラーに出す（ほかの成果物は出す。#110）。"""
    beam = tmp_path / "beam"
    beam.mkdir()
    (beam / "a.yml").write_text("name: a\nmetrics:\n  - name: m\n", encoding="utf-8")
    (beam / "b.yml").write_text("name: [\n", encoding="utf-8")
    (beam / "c.yml").write_text("title: name が無い\n", encoding="utf-8")
    (beam / "d.yml").write_text("name: d\ncreated: 2020-02-30\n", encoding="utf-8")
    errors: list[str] = []
    beams = DatasetIndex(tmp_path, None).beams(errors)
    assert [(b.name, b.metrics) for b in beams] == [("a", ["m"])]
    assert errors == [f"{n}.yml を読めませんでした" for n in "bcd"]


def test_sources_without_descriptions_use_ontology_labels(repo: Path) -> None:
    """説明の無い源泉・列は、オントロジーの日本語のラベルで埋める（画面に英語を出さない。#148）。

    開始画面（sources）とプロファイル画面（profile）で同じ。説明があればそちらを使う。
    """
    import shutil

    from adp.llm.settings import REPO_ROOT

    (repo / "ontology").mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "ontology" / "ontology.yml", repo / "ontology" / "ontology.yml")
    try:
        manifest = repo / "warehouse/target/manifest.json"
        catalog = Catalog(repo / "warehouse/data/dev.duckdb", manifest)
        index = DatasetIndex(repo, catalog)
        sources = {s.name: s for s in index.all().sources}
        inventory = sources["raw.inventory"]
        assert inventory.description == "在庫の入出庫" and not inventory.unmapped
        columns = {c.name: c.description for c in inventory.columns}
        assert columns["movement_type"] == "入出庫区分" and columns["quantity"] == "数量"
        profile = index.profile("raw.inventory")
        assert profile.description == "在庫の入出庫"
        assert {c.name: c.description for c in profile.columns}["movement_type"] == "入出庫区分"
        # 源泉の説明（dbt の source）があれば、そちらを使う
        orders = sources["raw.orders"]
        assert orders.description and orders.description != "注文"
    finally:
        shutil.rmtree(repo / "ontology")
    # オントロジーが無ければ埋めない（落ちない）
    plain = {
        s.name: s
        for s in DatasetIndex(repo, Catalog(repo / "warehouse/data/dev.duckdb")).all().sources
    }
    assert plain["raw.inventory"].description == ""
