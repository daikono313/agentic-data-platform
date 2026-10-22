"""オントロジー（業務の概念・属性・関係・制約。#67）。正本は `ontology/ontology.yml`。"""

from adp.ontology.models import (
    KIND_LABELS,
    Attribute,
    Concept,
    ConceptKind,
    Constraint,
    Domain,
    Ontology,
    Relation,
    dump,
    load,
)
from adp.ontology.render import markdown, mermaid

__all__ = [
    "Attribute",
    "KIND_LABELS",
    "Concept",
    "ConceptKind",
    "Constraint",
    "Domain",
    "Ontology",
    "Relation",
    "dump",
    "load",
    "markdown",
    "mermaid",
]
