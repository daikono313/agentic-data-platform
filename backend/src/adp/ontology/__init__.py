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
from adp.ontology.render import attribute_notes, markdown, mermaid, multiplicity

__all__ = [
    "attribute_notes",
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
    "multiplicity",
]
