"""Generate the metadata-only template questions (1-3 hops) with gold answers computed by SPARQL.

Each question names one topic entity (a film or a person), as in PoG and GCR. The SPARQL query that defines the
gold answer is stored with the question, so the answers can be re-checked on Fuseki. These questions need no plot
facts, so base and enriched graphs must score the same on them: they are the control set.
"""
import argparse
import logging
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rdflib
import requests

from p0_commons.config import Settings, load_settings, setup_logging, write_json

logger = logging.getLogger(__name__)

PREFIXES = """PREFIX schema: <http://schema.org/>
PREFIX kl: <http://example.org/kl/ontology#>
"""


@dataclass(frozen=True)
class Template:
    name: str
    hops: int
    text: str
    topic: str  # "film", "director" or "actor"
    pattern: str  # SPARQL pattern; {t} is the topic entity, ?answer the answer


TEMPLATES = [
    Template("film_director", 1, "Who directed {name}?", "film", "{t} schema:director ?answer ."),
    Template("film_actors", 1, "Which actors starred in {name}?", "film", "{t} schema:actor ?answer ."),
    Template("film_writers", 1, "Who wrote {name}?", "film", "{t} kl:writer ?answer ."),
    Template("film_genres", 1, "Which genres does {name} belong to?", "film", "{t} schema:genre ?answer ."),
    Template("director_films", 1, "Which films did {name} direct?", "director", "?answer schema:director {t} ."),
    Template("actor_films", 1, "Which films did {name} star in?", "actor", "?answer schema:actor {t} ."),
    Template(
        "director_actors", 2, "Which actors starred in films directed by {name}?", "director",
        "?film schema:director {t} ; schema:actor ?answer .",
    ),
    Template(
        "actor_directors", 2, "Who directed the films that {name} starred in?", "actor",
        "?film schema:actor {t} ; schema:director ?answer .",
    ),
    Template(
        "actor_costars", 2, "Which actors starred alongside {name}?", "actor",
        "?film schema:actor {t} , ?answer . FILTER (?answer != {t})",
    ),
    Template(
        "director_genres", 2, "Which genres appear in films directed by {name}?", "director",
        "?film schema:director {t} ; schema:genre ?answer .",
    ),
    Template(
        "film_same_director", 2, "Which other films were directed by the director of {name}?", "film",
        "{t} schema:director ?director . ?answer schema:director ?director . FILTER (?answer != {t})",
    ),
    Template(
        "film_actor_films", 2, "In which other films did the cast of {name} star?", "film",
        "{t} schema:actor ?actor . ?answer schema:actor ?actor . FILTER (?answer != {t})",
    ),
    Template(
        "film_cast_directors", 3, "Who directed the other films that the cast of {name} starred in?", "film",
        "{t} schema:actor ?actor . ?film schema:actor ?actor ; schema:director ?answer . FILTER (?film != {t})",
    ),
    Template(
        "film_director_other_actors", 3, "Which actors starred in the other films by the director of {name}?", "film",
        "{t} schema:director ?director . ?film schema:director ?director ; schema:actor ?answer . FILTER (?film != {t})",
    ),
    Template(
        "director_actor_films", 3, "Which films not directed by {name} star actors from films directed by {name}?", "director",
        "?own schema:director {t} ; schema:actor ?actor . ?answer schema:actor ?actor . FILTER NOT EXISTS { ?answer schema:director {t} }",
    ),
    Template(
        "actor_costar_films", 3, "Which films without {name} star actors who starred alongside {name}?", "actor",
        "?film schema:actor {t} , ?costar . FILTER (?costar != {t}) ?answer schema:actor ?costar . "
        "FILTER NOT EXISTS { ?answer schema:actor {t} }",
    ),
]

TOPIC_QUERIES = {
    "film": "SELECT DISTINCT ?e WHERE { ?e a kl:Film }",
    "director": "SELECT DISTINCT ?e WHERE { ?film schema:director ?e }",
    "actor": "SELECT DISTINCT ?e WHERE { ?film schema:actor ?e }",
}


def answer_query(template: Template, topic: str) -> str:
    pattern = template.pattern.replace("{t}", f"<{topic}>")
    return f"{PREFIXES}SELECT DISTINCT ?answer WHERE {{\n  {pattern}\n}}"


def unique_names(graph: rdflib.Graph) -> dict[str, str]:
    """IRI -> name for entities whose name no other film or person shares (ambiguous names are skipped)."""
    names: dict[str, str] = {}
    for subject, name in graph.query(f"{PREFIXES}SELECT ?s ?n WHERE {{ {{ ?s a kl:Film }} UNION {{ ?s a kl:Person }} ?s schema:name ?n }}"):
        names[str(subject)] = str(name)
    counts = Counter(name.casefold() for name in names.values())
    return {iri: name for iri, name in names.items() if counts[name.casefold()] == 1}


def generate(graph: rdflib.Graph, per_hop: int, max_answers: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    names = unique_names(graph)
    all_names = {str(s): str(n) for s, n in graph.query(f"{PREFIXES}SELECT ?s ?n WHERE {{ ?s schema:name ?n }}")}
    topics = {kind: sorted(str(row[0]) for row in graph.query(PREFIXES + query)) for kind, query in TOPIC_QUERIES.items()}
    by_hops: dict[int, list[Template]] = defaultdict(list)
    for template in TEMPLATES:
        by_hops[template.hops].append(template)

    questions = []
    for hops in sorted(by_hops):
        templates = by_hops[hops]
        candidates: dict[str, list[dict[str, Any]]] = {}
        for template in templates:
            pool = [topic for topic in topics[template.topic] if topic in names]
            rng.shuffle(pool)
            found = []
            for topic in pool:
                answers = sorted(str(row[0]) for row in graph.query(answer_query(template, topic)))
                if 1 <= len(answers) <= max_answers:
                    found.append({"template": template, "topic": topic, "answers": answers})
                if len(found) >= per_hop:
                    break
            candidates[template.name] = found
        # round-robin over templates so every template is represented before any repeats
        picked: list[dict[str, Any]] = []
        position = 0
        while len(picked) < per_hop and any(position < len(found) for found in candidates.values()):
            for template in templates:
                found = candidates[template.name]
                if position < len(found) and len(picked) < per_hop:
                    picked.append(found[position])
            position += 1
        for entry in picked:
            template, topic = entry["template"], entry["topic"]
            questions.append(
                {
                    "id": f"T{len(questions) + 1:03d}",
                    "template": template.name,
                    "hops": template.hops,
                    "question": template.text.format(name=names[topic]),
                    "topic_entities": [{"iri": topic, "label": names[topic]}],
                    "sparql": answer_query(template, topic),
                    "answers": [{"iri": answer, "label": all_names.get(answer, answer)} for answer in entry["answers"]],
                }
            )
    return questions


def verify_on_fuseki(questions: list[dict[str, Any]], settings: Settings) -> int:
    fuseki = settings["fuseki"]
    url = f"{settings.value('fuseki', 'url', env='FUSEKI_URL').rstrip('/')}/{fuseki['dataset']}/{fuseki['query_endpoint']}"
    graph = settings["graphs"]["base"]
    mismatches = 0
    session = requests.Session()
    for question in questions:
        query = question["sparql"].replace("WHERE {", f"FROM <{graph}> WHERE {{", 1)
        response = session.post(url, data={"query": query}, headers={"Accept": "application/sparql-results+json"}, timeout=60)
        response.raise_for_status()
        answers = sorted(binding["answer"]["value"] for binding in response.json()["results"]["bindings"])
        if answers != [answer["iri"] for answer in question["answers"]]:
            mismatches += 1
            logger.warning("%s differs on Fuseki", question["id"])
    return mismatches


def summary(questions: list[dict[str, Any]]) -> dict[str, Any]:
    sizes = [len(question["answers"]) for question in questions]
    return {
        "questions": len(questions),
        "by_hops": {str(hops): count for hops, count in sorted(Counter(q["hops"] for q in questions).items())},
        "by_template": dict(sorted(Counter(q["template"] for q in questions).items())),
        "answers_per_question_mean": round(sum(sizes) / len(sizes), 2) if sizes else None,
        "single_answer_questions": sum(size == 1 for size in sizes),
    }


def run(settings: Settings, graph_path: Path, verify: bool) -> Path:
    config = settings["benchmark"]
    graph = rdflib.Graph()
    graph.parse(graph_path)
    questions = generate(graph, config["questions_per_hop"], config["max_answers"], config["seed"])
    stats = summary(questions)
    if verify:
        stats["fuseki_mismatches"] = verify_on_fuseki(questions, settings)
    target = settings.path("benchmark") / "template_questions.json"
    write_json(target, {"graph": str(graph_path.relative_to(settings.root)).replace("\\", "/"), **config, "summary": stats, "questions": questions})
    write_json(settings.path("reports_benchmark") / "template_questions_summary.json", stats)
    logger.info("Wrote %d questions to %s: %s", len(questions), target, stats["by_hops"])
    if stats["questions"] < 3 * config["questions_per_hop"]:
        logger.warning("Fewer questions than requested: the snapshot is too small for some templates")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate metadata-only template questions with SPARQL gold answers.")
    parser.add_argument("--graph", type=Path, default=None, help="defaults to <paths.rdf>/base.ttl")
    parser.add_argument("--verify-fuseki", action="store_true", help="re-run every gold query on Fuseki and compare")
    args = parser.parse_args()
    setup_logging()
    settings = load_settings()
    run(settings, (args.graph or settings.path("rdf") / "base.ttl").resolve(), args.verify_fuseki)


if __name__ == "__main__":
    main()
