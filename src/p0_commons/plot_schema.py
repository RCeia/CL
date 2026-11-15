from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

import rdflib
from rdflib.namespace import RDFS, SKOS

PROMPT_SECTIONS = ("[system]", "[user]")


@dataclass(frozen=True)
class PlotRelation:
    name: str
    iri: str
    label: str
    scheme: str
    concepts: dict[str, str] = field(default_factory=dict)
    alt_labels: dict[str, str] = field(default_factory=dict)

    def resolve(self, label: str) -> str | None:
        key = label.strip().casefold()
        for pref, iri in self.concepts.items():
            if pref.casefold() == key:
                return iri
        return self.alt_labels.get(key)


def english(graph: rdflib.Graph, subject: rdflib.term.Node, predicate: rdflib.URIRef) -> list[str]:
    return sorted(str(value) for value in graph.objects(subject, predicate) if getattr(value, "language", None) in (None, "en"))


def load_relations(ontology: Path, vocabulary: Path, ontology_namespace: str) -> list[PlotRelation]:
    graph = rdflib.Graph()
    graph.parse(ontology)
    graph.parse(vocabulary)
    kl = rdflib.Namespace(ontology_namespace)
    relations = []
    for prop in graph.subjects(RDFS.subPropertyOf, kl.plotRelation):
        scheme = graph.value(prop, kl.valueScheme)
        if scheme is None:
            raise ValueError(f"{prop} has no kl:valueScheme")
        concepts: dict[str, str] = {}
        alt_labels: dict[str, str] = {}
        for concept in graph.subjects(SKOS.inScheme, scheme):
            for label in english(graph, concept, SKOS.prefLabel)[:1]:
                concepts[label] = str(concept)
            for label in english(graph, concept, SKOS.altLabel):
                alt_labels[label.casefold()] = str(concept)
        if not concepts:
            raise ValueError(f"{scheme} has no concepts")
        relations.append(
            PlotRelation(
                name=str(prop).removeprefix(ontology_namespace),
                iri=str(prop),
                label=english(graph, prop, RDFS.label)[0],
                scheme=str(scheme),
                concepts=dict(sorted(concepts.items(), key=lambda item: item[0].casefold())),
                alt_labels=alt_labels,
            )
        )
    return sorted(relations, key=lambda relation: relation.name)


def json_schema(relations: list[PlotRelation], max_values: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            relation.name: {"type": "array", "items": {"type": "string", "enum": list(relation.concepts)}, "maxItems": max_values}
            for relation in relations
        },
        "required": [relation.name for relation in relations],
        "additionalProperties": False,
    }


def relations_description(relations: list[PlotRelation]) -> str:
    return "\n".join(f"- {relation.name} ({relation.label}): {' | '.join(relation.concepts)}" for relation in relations)


def load_prompt(path: Path) -> tuple[Template, Template]:
    text = path.read_text(encoding="utf-8")
    system_marker, user_marker = PROMPT_SECTIONS
    if system_marker not in text or user_marker not in text:
        raise ValueError(f"{path} must contain {system_marker} and {user_marker} sections")
    system, user = text.split(user_marker, 1)
    return Template(system.replace(system_marker, "", 1).strip()), Template(user.strip())


def build_messages(
    templates: tuple[Template, Template],
    relations: list[PlotRelation],
    max_values: int,
    title: str,
    year: str,
    synopsis: str,
) -> list[dict[str, str]]:
    values = {
        "relations": relations_description(relations),
        "keys": ", ".join(relation.name for relation in relations),
        "max_values": str(max_values),
        "title": title,
        "year": year,
        "synopsis": synopsis,
    }
    system, user = templates
    return [{"role": "system", "content": system.substitute(values)}, {"role": "user", "content": user.substitute(values)}]


def validate_output(payload: Any, relations: list[PlotRelation], max_values: int) -> list[str]:
    if not isinstance(payload, dict):
        return ["output is not a JSON object"]
    errors = []
    expected = {relation.name: relation for relation in relations}
    for key in payload.keys() - expected.keys():
        errors.append(f"unexpected key {key}")
    for name, relation in expected.items():
        values = payload.get(name)
        if not isinstance(values, list):
            errors.append(f"{name} is missing or not a list")
            continue
        if len(values) > max_values:
            errors.append(f"{name} has {len(values)} values")
        errors.extend(f"{name} has unknown value {value!r}" for value in values if not isinstance(value, str) or value not in relation.concepts)
    return errors
