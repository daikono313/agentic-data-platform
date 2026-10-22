"""オントロジー（#67）: 型の検証・図の生成・源泉との照合。"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adp.catalog.catalog import Catalog
from adp.ontology import Ontology, dump, load, markdown, mermaid
from adp.ontology.__main__ import DOC, ONTOLOGY
from adp.ontology.check import beam_mismatches, check

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
    # 状態（定期スナップショット）の概念も fct_（ADR 0008）
    data["concepts"][1] |= {"kind": "state", "model": "dim_order"}
    with pytest.raises(ValidationError, match="fct_"):
        Ontology.model_validate(data)
    data["concepts"][1]["model"] = "fct_order_levels"
    assert Ontology.model_validate(data).concept("order") is not None


def test_mermaid_escapes_labels() -> None:
    """ラベルの引用符・山括弧は Mermaid の構文を壊さない（図への注入を防ぐ）。"""
    data = _data(constraints=[{"concept": "order", "text": 'a"] --> x["<script>'}])
    text = mermaid(Ontology.model_validate(data))
    line = next(x for x in text.splitlines() if x.strip().startswith("k_0["))
    assert line.strip() == 'k_0["a#quot;] --#gt; x[#quot;#lt;script#gt;"]:::constraint'
    assert "<script>" not in text
    assert 'c_customer -- "注文する" --> c_order' in text
    assert "c_order([" in text and "c_customer(" in text  # 出来事とものの形を変える
    data["concepts"][1]["kind"] = "state"
    text = mermaid(Ontology.model_validate(data))
    assert 'c_order{{"' in text and "class c_order state" in text  # 状態は六角形


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
    assert result.unmapped_models == ["dim_date"]  # ほかはエラーにだけ出す（2 回出さない）
    assert [e for e in result.errors if "fct_sales_copy" in e]
    assert not [e for e in result.errors if "dim_date" in e]


def test_beam_words_must_match_ontology(tmp_path: Path) -> None:
    """BEAM 成果物の属性のラベル（名前は見ない）を、オントロジーの属性と照合する（#137）。"""
    attrs = [
        {
            "name": "region",
            "label": "居住地方",
            "source": {"table": "raw.customers", "column": "region"},
        },
        {
            "name": "area",
            "label": "地方",
            "source": {"table": "raw.customers", "column": "region"},
        },
        {"name": "x", "label": "式", "source": {"table": "raw.customers", "expr": "1"}},
        {"name": "y", "label": "無い列", "source": {"table": "raw.customers", "column": "y"}},
    ]
    # 出来事をまたいで共通のディメンションは 1 回だけ（設計の検査と同じ）
    dims = [{"name": "customer", "attributes": attrs}]
    beam = {"events": [{"name": "e", "dimensions": dims}, {"name": "f", "dimensions": dims}]}
    (tmp_path / "b.yml").write_text(json.dumps(beam, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "broken.yml").write_text("events: [", encoding="utf-8")
    errors = beam_mismatches(Ontology.model_validate(_data()), tmp_path)
    assert errors[1:] == [
        "b.yml: customer.region: raw.customers.region は、オントロジーでは「顧客」の属性 region"
        "「地方」です。ラベルを「地方」にしてください"
    ]
    assert errors[0].startswith("broken.yml: 読めない")


def test_check_command_fails_on_beam_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m adp.ontology check` は BEAM 成果物の言葉の食い違いでも落ちる（#137）。"""
    from adp.ontology import __main__ as cli
    from adp.ontology.check import CheckResult

    ontology = Ontology.model_validate(_data())
    (tmp_path / "ontology").mkdir()
    (tmp_path / "ontology" / "ontology.yml").write_text(dump(ontology), encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ontology.md").write_text(markdown(ontology), encoding="utf-8")
    (tmp_path / "warehouse" / "target").mkdir(parents=True)
    (tmp_path / "warehouse" / "target" / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "warehouse" / "db.duckdb").write_text("", encoding="utf-8")
    (tmp_path / "beam").mkdir()
    attr = {
        "name": "area",
        "label": "居住地方",
        "source": {"table": "raw.customers", "column": "region"},
    }
    beam = {"events": [{"name": "e", "dimensions": [{"name": "c", "attributes": [attr]}]}]}
    (tmp_path / "beam" / "b.yml").write_text(json.dumps(beam, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "ONTOLOGY", tmp_path / "ontology" / "ontology.yml")
    monkeypatch.setattr(cli, "DOC", tmp_path / "docs" / "ontology.md")
    monkeypatch.setattr(cli, "warehouse_db", lambda w: w / "db.duckdb")
    monkeypatch.setattr(cli, "Catalog", lambda db: None)
    monkeypatch.setattr(
        cli,
        "check",
        lambda *a: CheckResult(
            errors=[], unmapped_sources=[], unmodeled_attributes=["dim_c.x（X）"]
        ),
    )
    assert cli.main(["check"]) == 1
    out = capsys.readouterr().out
    assert "✗ b.yml: c.area: raw.customers.region は" in out
    assert "・dim に列の無い属性（要求で使われたら生成が足す）: dim_c.x（X）" in out


def test_repository_beam_words_match_ontology() -> None:
    assert beam_mismatches(load(ONTOLOGY), REPO / "beam") == []


def test_attributes_missing_from_the_dim_are_listed(tmp_path: Path) -> None:
    """もの・人・場所の概念の属性で、dim に列が無いものは表示する（食い違いにはしない。#137）。"""
    db = _db(tmp_path, "order_id int, customer_id int")
    manifest = tmp_path / "manifest.json"
    node = {"name": "dim_customer", "resource_type": "model", "columns": {"customer_key": {}}}
    manifest.write_text(
        json.dumps(
            {
                "nodes": {"model.p.dim_customer": node},
                "sources": {"source.p.raw.customers": {"schema": "raw", "name": "customers"}},
                "parent_map": {"model.p.dim_customer": ["source.p.raw.customers"]},
            }
        )
    )
    result = check(Ontology.model_validate(_data()), Catalog(db), manifest)
    assert result.unmodeled_attributes == ["dim_customer.region（地方）"]
    assert not [e for e in result.errors if "region" in e]


def test_same_as_names_the_canonical_attribute() -> None:
    """同じ業務の言葉を別の源泉にも持つときは same_as で正本を示す（#137）。"""
    data = _data()
    order = data["concepts"][1]
    order["attributes"] = [{"name": "area", "label": "地方", "column": "region"}]
    onto = Ontology.model_validate(data)
    assert onto.duplicate_labels() == [
        "言葉「地方」（ラベルか同義語）の属性が別々に定義されている（customer.region、order.area）。"
        "同じものなら片方に same_as で正本を書き、違うものなら言葉を分ける"
    ]
    order["attributes"][0]["same_as"] = "customer.region"
    onto = Ontology.model_validate(data)
    assert onto.duplicate_labels() == []
    assert "地方（`region`。正本は顧客の地方）" in markdown(onto)
    for target, label, message in [
        ("customer.nope", "地方", "ほかの概念の属性に無い"),
        ("order.area", "地方", "ほかの概念の属性に無い"),
        ("order.status", "地方", "ほかの概念の属性に無い"),
        ("customer.region", "居住地", "と違う"),
    ]:
        order["attributes"][0].update(same_as=target, label=label)
        with pytest.raises(ValidationError, match=message):
            Ontology.model_validate(data)
    order["attributes"] = [
        {"name": "area", "label": "地方", "column": "region", "same_as": "customer.region"}
    ]
    data["concepts"][0]["attributes"].append(
        {"name": "area2", "label": "地方", "column": "region2", "same_as": "order.area"}
    )
    with pytest.raises(ValidationError, match="正本ではない"):
        Ontology.model_validate(data)


def test_expr_column_only_for_value_replacements() -> None:
    """値を置き換えるだけの式だけ元の列を返す。列から別の言葉を作る式（年代・価格帯）は見ない。"""
    onto = Ontology.model_validate(_data())
    assert onto.expr_column("raw.customers", "coalesce(region, '地方なし')") == "region"
    assert onto.expr_column("raw.customers", " IFNULL(region, 0) ") == "region"
    assert onto.expr_column("raw.customers", "region") == "region"
    for derived in [
        "upper('region')",
        "case when region = '関東' then '首都圏' else 'その他' end",
        "date_diff('year', region, current_date)",
        "coalesce(region, other)",
    ]:
        assert onto.expr_column("raw.customers", derived) is None, derived
    assert onto.expr_column("raw.orders", "region") is None


def test_beam_words_check_single_column_expressions(tmp_path: Path) -> None:
    """式で作る属性も、式が使う属性の列が 1 つなら照合する（#137）。"""
    attr = {
        "name": "region",
        "label": "居住地方",
        "source": {"table": "raw.customers", "expr": "coalesce(region, '不明')"},
    }
    beam = {"events": [{"name": "e", "dimensions": [{"name": "c", "attributes": [attr]}]}]}
    (tmp_path / "b.yml").write_text(json.dumps(beam, ensure_ascii=False), encoding="utf-8")
    assert beam_mismatches(Ontology.model_validate(_data()), tmp_path) == [
        "b.yml: c.region: raw.customers.region は、オントロジーでは「顧客」の属性 region"
        "「地方」です。ラベルを「地方」にしてください"
    ]


def test_dim_descriptions_start_with_the_label(tmp_path: Path) -> None:
    """dim の列の説明は、オントロジーの属性のラベルで始める（補足は「（…）」で後ろに。#137）。"""
    db = _db(tmp_path, "order_id int, customer_id int")
    manifest = tmp_path / "manifest.json"

    def run(description: str) -> list[str]:
        node = {
            "name": "dim_customer",
            "resource_type": "model",
            "columns": {"region": {"description": description}},
        }
        manifest.write_text(
            json.dumps(
                {
                    "nodes": {"model.p.dim_customer": node},
                    "sources": {"source.p.raw.customers": {"schema": "raw", "name": "customers"}},
                    "parent_map": {"model.p.dim_customer": ["source.p.raw.customers"]},
                }
            )
        )
        result = check(Ontology.model_validate(_data()), Catalog(db), manifest)
        return [e for e in result.errors if "説明" in e]

    assert run("地方") == [] and run("地方（不明は「不明」）") == []
    assert run("居住地方") == [
        "概念 customer の属性 region: dim_customer.region の説明「居住地方」が、ラベル「地方」"
        "（か「地方（補足）」）になっていない"
    ]


def test_duplicate_labels_fail_the_check(tmp_path: Path) -> None:
    db = _db(tmp_path, "order_id int, customer_id int, region text")
    data = _data()
    data["concepts"][1]["attributes"] = [{"name": "region", "label": "地方", "column": "region"}]
    result = check(Ontology.model_validate(data), Catalog(db), None)
    assert [e for e in result.errors if e.startswith("言葉「地方」")]


def test_synonyms_must_not_name_another_attribute(tmp_path: Path) -> None:
    """同義語は、ほかの属性のラベル・同義語と重ねない（同じ言葉が 2 つの属性を指さない。#137）。
    BEAM で同義語を使ったときは、正本のラベルにそろえるよう案内する。"""
    data = _data()
    data["concepts"][0]["attributes"] = [
        {"name": "region", "label": "居住地方", "column": "region", "synonyms": ["地方"]}
    ]
    data["concepts"][1]["attributes"] = [
        {"name": "status", "label": "地方", "column": "order_status"}
    ]
    ontology = Ontology.model_validate(data)
    assert any(e.startswith("言葉「地方」") for e in ontology.duplicate_labels())
    mismatch = ontology.word_mismatch("raw.customers", "region", "地方")
    assert mismatch is not None and "（「地方」は同義語）" in mismatch

    for words in (["居住地方"], ["地方", "地方"], [""]):
        data["concepts"][0]["attributes"][0]["synonyms"] = words
        with pytest.raises(ValidationError, match="同義語"):
            Ontology.model_validate(data)


def test_hierarchy_parent_is_checked_against_data(tmp_path: Path) -> None:
    """階層（parent）は同じ概念の属性で、循環しない。子の値ごとに親の値は 1 つ（#137）。"""
    import duckdb

    db = tmp_path / "h.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute("create table raw.customers (customer_id int, prefecture varchar, region varchar)")
    con.execute(
        "insert into raw.customers values (1, '東京都', '関東'), (2, '東京都', '関東'),"
        " (3, '大阪府', '近畿'), (4, null, '近畿')"
    )
    con.execute("create table raw.orders (order_id int, customer_id int)")
    con.execute("insert into raw.orders values (10, 1)")
    con.close()
    data = _data()
    data["concepts"][0]["attributes"] = [
        {"name": "prefecture", "label": "都道府県", "column": "prefecture", "parent": "region"},
        {"name": "region", "label": "地方", "column": "region"},
    ]
    assert check(Ontology.model_validate(data), Catalog(db), None).errors == []
    assert "上の階層は地方" in markdown(Ontology.model_validate(data))

    con = duckdb.connect(str(db))
    con.execute("insert into raw.customers values (5, '東京都', '近畿')")
    con.close()
    [error] = check(Ontology.model_validate(data), Catalog(db), None).errors
    assert "階層の親 region の値が 2 つ以上ある都道府県が 1 件" in error
    # 親が空の行も 1 つの値として数える（大阪府に、近畿の行と空の行がある）
    con = duckdb.connect(str(db))
    con.execute("delete from raw.customers where customer_id = 5")
    con.execute("insert into raw.customers values (6, '大阪府', null)")
    con.close()
    [error] = check(Ontology.model_validate(data), Catalog(db), None).errors
    assert "都道府県が 1 件" in error

    data["concepts"][0]["attributes"][1]["parent"] = "prefecture"
    with pytest.raises(ValidationError, match="循環"):
        Ontology.model_validate(data)
    data["concepts"][0]["attributes"][1]["parent"] = "no_such"
    with pytest.raises(ValidationError, match="parent no_such が無い"):
        Ontology.model_validate(data)


def test_role_is_checked_against_column_type(tmp_path: Path) -> None:
    """役割（role）は列の型と照合する。文字列の列からは役割が分からないので見ない（#137）。"""
    import duckdb

    db = tmp_path / "r.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute(
        "create table raw.customers (customer_id int, signup_date date, points int, memo varchar)"
    )
    con.execute("insert into raw.customers values (1, '2024-01-01', 3, 'x')")
    con.execute("create table raw.orders (order_id int, customer_id int)")
    con.execute("insert into raw.orders values (10, 1)")
    con.close()
    data = _data()
    data["concepts"][0]["attributes"] = [
        {"name": "signup_date", "label": "登録日", "column": "signup_date", "role": "temporal"},
        {"name": "points", "label": "ポイント数", "column": "points", "role": "numeric"},
        {"name": "memo", "label": "メモ", "column": "memo", "role": "numeric"},
    ]
    assert check(Ontology.model_validate(data), Catalog(db), None).errors == []
    data["concepts"][0]["attributes"][1]["role"] = "temporal"
    [error] = check(Ontology.model_validate(data), Catalog(db), None).errors
    assert "役割が時刻（temporal）なのに、列 raw.customers.points の型は INTEGER" in error
    # 文字列以外で役割の分からない型（真偽値など）は、数値・時刻と食い違う
    con = duckdb.connect(str(db))
    con.execute("alter table raw.customers add column active boolean")
    con.close()
    data["concepts"][0]["attributes"][1]["role"] = "numeric"
    data["concepts"][0]["attributes"].append(
        {"name": "active", "label": "有効", "column": "active", "role": "numeric"}
    )
    [error] = check(Ontology.model_validate(data), Catalog(db), None).errors
    assert "列 raw.customers.active の型は BOOLEAN" in error


def test_relation_multiplicity_is_checked_against_data(tmp_path: Path) -> None:
    """関係の多重度（1 対 1 なら via が重複しない）と、相手を必ず持つか（optional: false なら via に
    空が無い）を、源泉の値と照合する。宣言していないものは見ない（#137）。"""
    import duckdb

    db = _db(tmp_path, "order_id int, customer_id int")
    con = duckdb.connect(str(db))
    con.execute("update raw.orders set customer_id = 1")
    con.close()
    data = _data()
    relation = data["relations"][0]
    assert check(Ontology.model_validate(data), Catalog(db), None).errors == []

    relation |= {"cardinality": "one_to_many", "optional": False}
    assert check(Ontology.model_validate(data), Catalog(db), None).errors == []
    assert "| 顧客 1 件に注文が複数、注文には必ず顧客がある |" in markdown(
        Ontology.model_validate(data)
    )

    relation["cardinality"] = "one_to_one"
    [error] = check(Ontology.model_validate(data), Catalog(db), None).errors
    assert (
        "顧客 1 件に注文は 1 件まで（one_to_one）としているのに、列 raw.orders.customer_id の値が"
        "重複している" in error
    )

    con = duckdb.connect(str(db))
    con.execute("update raw.orders set customer_id = null where order_id = 11")
    con.close()
    relation["cardinality"] = "one_to_many"
    [error] = check(Ontology.model_validate(data), Catalog(db), None).errors
    assert (
        "注文には必ず顧客がある（optional: false）としているのに、列 raw.orders.customer_id が"
        "空の行が 1 行" in error
    )
    relation["optional"] = True
    assert check(Ontology.model_validate(data), Catalog(db), None).errors == []
    assert "顧客が決まっていない注文がある" in markdown(Ontology.model_validate(data))


def test_new_fields_round_trip_and_are_omitted_when_unset(tmp_path: Path) -> None:
    """未確認の項目（役割・階層・同義語・多重度）は、書き直しても yml に出さない（取り込みと生成が
    正本を書き直すので、差分を増やさない）。値があれば、書き直し（link_models）の後も残る。"""
    from adp.ontology.models import link_models

    body = dump(Ontology.model_validate(_data())).split("version: 1", 1)[1]
    for key in ("role:", "parent:", "synonyms:", "cardinality:", "optional:"):
        assert key not in body

    data = _data()
    del data["concepts"][0]["model"]
    data["concepts"][0]["attributes"] = [
        {
            "name": "region",
            "label": "地方",
            "column": "region",
            "role": "descriptive",
            "synonyms": ["エリア"],
        },
        {"name": "prefecture", "label": "都道府県", "column": "prefecture", "parent": "region"},
    ]
    data["relations"][0] |= {"cardinality": "one_to_many", "optional": True}
    linked, names = link_models(
        Ontology.model_validate(data), [("raw.customers", "dim_customer", "entity")]
    )
    assert names == ["customer"]
    path = tmp_path / "o.yml"
    path.write_text(dump(linked), encoding="utf-8")
    again = load(path)
    assert again == linked
    assert again.concepts[0].attributes[0].synonyms == ["エリア"]
    assert again.concepts[0].attributes[1].parent == "region"
    assert again.relations[0].optional is True


def test_same_as_attribute_inherits_synonyms_and_role() -> None:
    """same_as の属性には同義語・役割を書かない（正本のものを使う。写しとずれないように）。"""
    data = _data()
    data["concepts"][1]["attributes"] = [
        {
            "name": "area",
            "label": "地方",
            "column": "region",
            "same_as": "customer.region",
            "synonyms": ["エリア"],
        }
    ]
    with pytest.raises(ValidationError, match="同義語・役割は正本のほうに書く"):
        Ontology.model_validate(data)


def test_multiplicity_direction_follows_the_column() -> None:
    """via を持つ側が「多」。via が from 側の源泉にあれば、to がキーを持つ側
    （店舗 1 : 注文 多）。"""
    data = _data()
    data["concepts"].append(
        {
            "name": "store",
            "label": "店舗",
            "domain": "sales",
            "kind": "entity",
            "source": "raw.stores",
            "key": "store_id",
        }
    )
    data["relations"] = [
        {
            "from": "order",
            "to": "store",
            "verb": "受け付けられる",
            "via": "raw.orders.store_id",
            "cardinality": "one_to_many",
        }
    ]
    assert "| 店舗 1 件に注文が複数 |" in markdown(Ontology.model_validate(data))


def test_relation_ends_use_the_key_column_when_sources_are_shared() -> None:
    """2 つの概念が同じ源泉でも、via の列がどちらのキーかで向きを決める（照合と表示で同じ判定。
    Ontology.relation_ends）。"""
    from adp.ontology import multiplicity

    data = _data()
    data["concepts"].append(
        {
            "name": "order_customer",
            "label": "注文した顧客",
            "domain": "sales",
            "kind": "entity",
            "source": "raw.orders",
            "key": "customer_id",
        }
    )
    data["relations"] = [
        {
            "from": "order",
            "to": "order_customer",
            "verb": "注文される",
            "via": "raw.orders.customer_id",
            "cardinality": "one_to_many",
            "optional": False,
        }
    ]
    ontology = Ontology.model_validate(data)
    ends = ontology.relation_ends(ontology.relations[0])
    assert ends is not None and [c.name for c in ends] == ["order_customer", "order"]
    assert multiplicity(ontology.relations[0], ontology) == (
        "注文した顧客 1 件に注文が複数、注文には必ず注文した顧客がある"
    )
