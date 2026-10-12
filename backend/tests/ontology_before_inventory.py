"""在庫（raw.inventory）を取り込む前のオントロジー（テスト用）。

main では在庫は取り込み済み（#109）。取り込みのテストは「後から増えた源泉」として在庫を使うので、
オントロジーから在庫の概念と、それに関わる関係・制約・ドメイン（inventory）を外したものを使う。
外した部分は、取り込みのエージェントが出す追加案（inventory_patch）に使う（定義を 1 か所に）。
"""

from pathlib import Path
from typing import Any

import yaml

from adp.ontology import Ontology
from adp.ontology.models import dump

REPO = Path(__file__).resolve().parents[2]
SOURCE = "raw.inventory"
DOMAIN = "inventory"


def _split() -> tuple[dict[str, Any], dict[str, Any]]:
    """(取り込む前のオントロジー, 在庫の追加案)。"""
    data: dict[str, Any] = yaml.safe_load(
        (REPO / "ontology" / "ontology.yml").read_text(encoding="utf-8")
    )
    removed = {c["name"] for c in data["concepts"] if c["source"] == SOURCE}
    concepts = [
        {k: v for k, v in c.items() if k != "model"}  # 追加案はモデルを持たない（生成が書き込む）
        for c in data["concepts"]
        if c["name"] in removed
    ]
    data["concepts"] = [c for c in data["concepts"] if c["name"] not in removed]
    used = {c["domain"] for c in data["concepts"]}
    domains = [d for d in data["domains"] if d["name"] == DOMAIN and DOMAIN not in used]
    data["domains"] = [d for d in data["domains"] if d not in domains]
    relations = data.get("relations", [])
    patch_relations = [r for r in relations if r["from"] in removed or r["to"] in removed]
    data["relations"] = [r for r in relations if r not in patch_relations]
    constraints = data.get("constraints", [])
    patch_constraints = [c for c in constraints if c["concept"] in removed]
    data["constraints"] = [c for c in constraints if c not in patch_constraints]
    patch = {
        "domains": domains,
        "concepts": concepts,
        "relations": patch_relations,
        "constraints": patch_constraints,
    }
    return data, patch


def ontology_data() -> dict[str, Any]:
    return _split()[0]


def inventory_patch() -> dict[str, Any]:
    """在庫の追加案（ontology.yml の在庫の部分から作る。モデルは除く）。"""
    return _split()[1]


def ontology() -> Ontology:
    return Ontology.model_validate(ontology_data())


def ontology_yaml() -> str:
    return dump(ontology())
