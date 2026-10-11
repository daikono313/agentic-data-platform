"""オントロジー（#67）: 型の検証・図の生成・源泉との照合。"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adp.catalog.catalog import Catalog
from adp.ontology import Ontology, dump, load, markdown, mermaid
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
    # 正本は dump の形（取り込み・生成が書き直しても、追加した行だけが差分になる。#69）
    assert ONTOLOGY.read_text(encoding="utf-8") == dump(onto)
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


@pytest.mark.parametrize(
    "text",
    [
        "a %%{init: {'theme':'forest','themeCSS':'svg{background:url(https://e.x/a)}'}}%% b",
        "行1\n%% コメントで後ろを消す",
        "`**md**` と #lt; の実体参照",
    ],
)
def test_mermaid_neutralizes_directives_comments_and_entities(text: str) -> None:
    """設定（%%{init}%%）・コメント・markdown・実体参照の文字を無効にする（#85 レビュー）。"""
    data = _data(constraints=[{"concept": "order", "text": text}])
    out = mermaid(Ontology.model_validate(data))
    line = next(x for x in out.splitlines() if x.strip().startswith("k_0["))
    assert "\n" not in line and "%" not in line and "{" not in line.split("[", 1)[1]
    assert "`" not in line and "#lt;" not in line.replace("#35;lt;", "")


def test_markdown_cells_are_sanitized_like_pr_body() -> None:
    """docs/ontology.md の表のセルも PR 本文と同じく無害化する（#110）。"""
    data = _data(
        constraints=[
            {"concept": "order", "text": "Closes #12 cc @someone /Users/me/x.txt ```a``` | b\nc"}
        ]
    )
    out = markdown(Ontology.model_validate(data))
    row = next(x for x in out.splitlines() if x.startswith("| 注文 | Closes"))
    assert "#\u200b12" in row and "@\u200bsomeone" in row
    assert "/Users/me" not in row and "<path>" in row
    assert "```" not in row and "\\|" in row and "\n" not in row


def test_duplicates_are_rejected() -> None:
    data = _data()
    data["relations"].append(dict(data["relations"][0]))
    with pytest.raises(ValidationError, match="同じ関係"):
        Ontology.model_validate(data)
    data = _data()
    data["concepts"][1]["model"] = "fct_x"
    data["concepts"].append({**data["concepts"][1], "name": "order2"})
    with pytest.raises(ValidationError, match="複数の概念"):
        Ontology.model_validate(data)


def test_yaml_syntax_error_is_value_error(tmp_path: Path) -> None:
    path = tmp_path / "ontology.yml"
    path.write_text("domains: [\n  - x")
    with pytest.raises(ValueError, match="YAML"):
        load(path)


def _db(tmp_path: Path, orders: str) -> Path:
    import duckdb

    db = tmp_path / "x.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute("create table raw.customers (customer_id int, region varchar)")
    con.execute("insert into raw.customers values (1, 'a'), (2, 'b')")
    con.execute(f"create table raw.orders ({orders})")
    con.execute("insert into raw.orders (order_id) values (10), (11)")
    con.execute("create table raw.returns (return_id int)")
    con.close()
    return db


def test_check_against_catalog(tmp_path: Path) -> None:
    db = _db(tmp_path, "order_id int")  # customer_id が無い
    result = check(Ontology.model_validate(_data()), Catalog(db), None)
    assert result.errors == ["関係「注文する」: 列 raw.orders.customer_id が無い"]
    assert result.unmapped_sources == ["raw.returns"]


def test_check_relation_column_must_be_the_other_key(tmp_path: Path) -> None:
    db = _db(tmp_path, "order_id int, customer_id int, order_status varchar")
    data = _data()
    data["relations"][0]["via"] = "raw.orders.order_status"
    result = check(Ontology.model_validate(data), Catalog(db), None)
    assert len(result.errors) == 1 and "order_status" in result.errors[0]


def test_check_key_must_be_unique(tmp_path: Path) -> None:
    import duckdb

    db = _db(tmp_path, "order_id int, customer_id int")
    con = duckdb.connect(str(db))
    con.execute("insert into raw.customers values (1, 'dup')")
    con.close()
    result = check(Ontology.model_validate(_data()), Catalog(db), None)
    assert result.errors == ["概念 customer: キー raw.customers.customer_id が一意でない"]


def test_check_model_lineage(tmp_path: Path) -> None:
    """概念のモデルが、概念の源泉を（staging 経由で）読んでいるか。"""
    db = _db(tmp_path, "order_id int, customer_id int")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "nodes": {
                    "model.p.dim_customer": {"name": "dim_customer", "resource_type": "model"},
                    "model.p.stg_orders": {"name": "stg_orders", "resource_type": "model"},
                    "model.p.dim_date": {"name": "dim_date", "resource_type": "model"},
                },
                "sources": {"source.p.raw.orders": {"schema": "raw", "name": "orders"}},
                "parent_map": {
                    "model.p.dim_customer": ["model.p.stg_orders"],
                    "model.p.stg_orders": ["source.p.raw.orders"],
                },
            }
        )
    )
    result = check(Ontology.model_validate(_data()), Catalog(db), manifest)
    assert result.errors == [
        "概念 customer: モデル dim_customer は源泉 raw.customers を読んでいない"
        "（読んでいる源泉: raw.orders）"
    ]
    assert result.unmapped_models == ["dim_date"]

    manifest.write_text('{"nodes": {}}')
    result = check(Ontology.model_validate(_data()), Catalog(db), manifest)
    assert "概念 customer: モデル dim_customer が dbt に無い" in result.errors


def test_cli_without_db_explains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from adp.ontology import __main__ as cli

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "DOC", tmp_path / "ontology.md")
    (tmp_path / "ontology.md").write_text(markdown(load(ONTOLOGY)), encoding="utf-8")
    assert cli.main(["check"]) == 1
    assert "make dbt-build を先に" in capsys.readouterr().out


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


def test_model_without_concept_is_an_error(tmp_path: Path) -> None:
    """どの概念にも結び付かない fact / dim はエラー（用途の重複を防ぐ。#114）。暦は対象外。"""
    import json

    import duckdb

    db = tmp_path / "x.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.close()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "nodes": {
                    "model.p.dim_date": {"resource_type": "model", "name": "dim_date"},
                    "model.p.fct_sales_copy": {"resource_type": "model", "name": "fct_sales_copy"},
                },
                "sources": {},
                "parent_map": {"model.p.dim_date": [], "model.p.fct_sales_copy": []},
            }
        )
    )
    ontology = Ontology.model_validate({"version": 1, "domains": [], "concepts": []})
    result = check(ontology, Catalog(db), manifest)
    assert result.unmapped_models == ["dim_date", "fct_sales_copy"]
    assert [e for e in result.errors if "fct_sales_copy" in e]
    assert not [e for e in result.errors if "dim_date" in e]
