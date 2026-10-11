"""オントロジー（#67）: 型の検証・図の生成・源泉との照合。"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adp.catalog.catalog import Catalog
from adp.ontology import Ontology, load, markdown, mermaid
from adp.ontology.__main__ import DOC, ONTOLOGY
from adp.ontology.check import check

REPO = Path(__file__).resolve().parents[3]


def _data(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "domains": [{"name": "sales", "label": "販売"}],
        "concepts": [
            {
                "name": "customer",
                "label": "顧客",
                "domain": "sales",
                "kind": "entity",
                "source": "raw.customers",
                "key": "customer_id",
                "model": "dim_customer",
                "attributes": [{"name": "region", "label": "地方", "column": "region"}],
            },
            {
                "name": "order",
                "label": "注文",
                "domain": "sales",
                "kind": "event",
                "source": "raw.orders",
                "key": "order_id",
            },
        ],
        "relations": [
            {"from": "customer", "to": "order", "verb": "注文する", "via": "raw.orders.customer_id"}
        ],
        "constraints": [{"concept": "order", "text": "キャンセルを含む"}],
    }
    data.update(over)
    return data


def test_repository_ontology_is_valid_and_doc_is_current() -> None:
    """正本が読め、docs/ontology.md が正本からの生成物と一致する（make ontology 忘れを防ぐ）。"""
    onto = load(ONTOLOGY)
    assert {c.kind for c in onto.concepts} == {"entity", "event"}
    assert DOC.read_text(encoding="utf-8") == markdown(onto)


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"domains": [{"name": "x", "label": "X"}]}, "ドメイン sales"),
        (
            {"relations": [{"from": "customer", "to": "nope", "verb": "v", "via": "raw.a.b"}]},
            "nope",
        ),
        ({"constraints": [{"concept": "nope", "text": "t"}]}, "nope"),
        ({"unknown": 1}, "Extra inputs"),
    ],
)
def test_invalid_references_are_rejected(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Ontology.model_validate(_data(**over))


def test_model_must_match_kind() -> None:
    data = _data()
    data["concepts"][1]["model"] = "dim_order"
    with pytest.raises(ValidationError, match="fct_"):
        Ontology.model_validate(data)


def test_mermaid_escapes_labels() -> None:
    """ラベルの引用符・山括弧は Mermaid の構文を壊さない（図への注入を防ぐ）。"""
    data = _data(constraints=[{"concept": "order", "text": 'a"] --> x["<script>'}])
    text = mermaid(Ontology.model_validate(data))
    line = next(x for x in text.splitlines() if x.strip().startswith("k_0["))
    assert line.strip() == 'k_0["a#quot;] --#gt; x[#quot;#lt;script#gt;"]:::constraint'
    assert "<script>" not in text
    assert 'c_customer -- "注文する" --> c_order' in text
    assert "c_order([" in text and "c_customer(" in text  # 出来事とものの形を変える


def test_check_against_catalog(tmp_path: Path) -> None:
    import duckdb

    db = tmp_path / "x.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute("create table raw.customers (customer_id int, region varchar)")
    con.execute("create table raw.orders (order_id int)")  # customer_id が無い
    con.execute("create table raw.returns (return_id int)")
    con.close()
    result = check(Ontology.model_validate(_data()), Catalog(db), None)
    assert result.errors == ["関係「注文する」: 列 raw.orders.customer_id が無い"]
    assert result.unmapped_sources == ["raw.returns"]

    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"nodes": {}}')
    result = check(Ontology.model_validate(_data()), Catalog(db), manifest)
    assert "概念 customer: モデル dim_customer が dbt に無い" in result.errors


def test_check_repository_ontology() -> None:
    """リポジトリの正本が、サンプルの源泉と dbt のモデルに合っている。"""
    warehouse = REPO / "warehouse"
    if not (warehouse / "target" / "manifest.json").exists():
        pytest.skip("make dbt-build が必要")
    result = check(
        load(ONTOLOGY),
        Catalog(warehouse / "data" / "dev.duckdb"),
        warehouse / "target" / "manifest.json",
    )
    assert result.errors == []
