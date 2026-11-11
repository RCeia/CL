# Enriching a Movie Knowledge Graph with LLM-Extracted Plot Facts

Knowledge and Language 2026/2027. A base knowledge graph is built from a TMDB snapshot linked to Wikidata, then enriched with plot facts extracted from synopses by an LLM, each annotated with a self-consistency confidence. The base and enriched graphs live side by side in Apache Jena Fuseki as separate named graphs.

| Module | Content | Status |
|---|---|---|
| 1 | TMDB collection, Wikidata alignment, mapping input, throughput pilot | implemented |
| 2 | Ontology, SKOS plot vocabulary, YARRRML/RML mapping, base KG, SHACL, Fuseki | implemented |
| 3 | Plot-fact extraction with 5 samples, confidence, enriched graph | schema, prompt and LLM client drafted for the pilot |
| 4 | Annotation sampling, Cohen's kappa, precision/recall, calibration | pending |
| 5 | 300-question metadata benchmark with SPARQL-verified gold answers | pending |

## Setup

Requirements: Python 3.11+, Docker with Compose (for Fuseki), and a CUDA GPU for the LLM.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

Fill `TMDB_API_KEY` in `.env` with either a v3 API key or a v4 read access token; the client detects which one it is. Every other parameter lives in `config/settings.yaml`, and the variables in `.env.example` override the matching settings (`WIKIDATA_CONTACT`, `FUSEKI_URL`, `LLM_BASE_URL`). Set `KLKG_CONFIG` to use a settings file other than `config/settings.yaml`.

## Pipeline

### 1. Snapshot and alignment

```bash
.venv/bin/klkg-collect
.venv/bin/klkg-align
```

`klkg-collect` pages through `/discover/movie` sorted by `vote_count.desc` within the configured years, keeps movies with an `en-US` overview, and stores one `/movie/{id}` response per candidate (with credits, keywords and external IDs) under `data/raw/tmdb/`. The candidate list is frozen in `data/raw/tmdb/manifest.json`; rerunning resumes missing downloads, and `--refresh` starts a new snapshot.

`klkg-align` resolves Wikidata IDs in batched SPARQL queries: films by P4947 (falling back to TMDB `external_ids`), people by P4985, countries by P297, languages by P218, plus the films' P840 narrative locations for later evaluation. Results are cached in `data/raw/wikidata/` together with `alignment_report.json`.

### 2. Throughput pilot

The pilot runs the draft extraction on the first 100 synopses with 5 samples each and projects the extraction time for N = 2,000, 2,500 and 3,000.

The development laptop has an 8 GB RTX 2070 (Turing), which has no bf16 support, so the model is served as AWQ 4-bit in fp16. Install vLLM in its own environment:

```bash
python3 -m venv .venv-llm
.venv-llm/bin/pip install -e ".[llm]"
.venv-llm/bin/vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ --dtype float16 --max-model-len 4096 --gpu-memory-utilization 0.90 --port 8000
```

Then, in another terminal:

```bash
.venv/bin/klkg-pilot
```

The report lands in `reports/pilot/` and the raw samples in `data/extraction/runs/`. On a 24 GB GPU, set `llm.model` to `Qwen/Qwen2.5-7B-Instruct` and serve it in bf16. The client speaks the OpenAI-compatible API with JSON-schema guided decoding, so other servers work as well: for Ollama, set `llm.base_url` to `http://localhost:11434/v1`, `llm.model` to the Ollama tag, and `llm.supports_n` to `false` (one request per sample).

### 3. Mapping input

Set `dataset.final_size` to the N chosen from the pilot (`null` keeps every candidate), then:

```bash
.venv/bin/klkg-prepare
```

This writes nested JSON to `data/interim/`. Every cast, crew, genre, keyword, company, country and language record carries its `movie_id`, because JSONPath cannot reach a parent element. `keyword_frequencies.json` lists the snapshot's keywords by frequency, to seed the curation of the SKOS vocabulary.

### 4. Base KG and validation

```bash
.venv/bin/klkg-build-base
.venv/bin/klkg-validate
```

`klkg-build-base` regenerates `mappings/base.rml.ttl` from `mappings/base.yarrrml.yml`, runs morph-kgc with `mappings/morph_kgc.ini`, and writes `data/rdf/base.ttl` plus `base_stats.json`. `klkg-validate` checks the graph against `ontology/shapes/base_shapes.ttl`, writes the report to `reports/shacl/`, and exits with status 1 if any violation is found (warnings, such as a film without a director, do not fail it).

### 5. Fuseki

```bash
docker compose -f fuseki/docker-compose.yml up -d --build
.venv/bin/klkg-load
```

The image downloads Fuseki 6.2.0 from the Apache archive and checks its SHA-512. The TDB2 dataset `kl` has `unionDefaultGraph` enabled and listens only on `127.0.0.1:3030`. Its endpoints are `/kl/sparql` (query), `/kl/update` and `/kl/data` (Graph Store Protocol). `klkg-load` replaces these named graphs:

| Graph | Content |
|---|---|
| `http://example.org/graph/ontology` | `kl_ontology.ttl` and `plot_vocabulary.ttl` |
| `http://example.org/graph/base` | `data/rdf/base.ttl` |
| `http://example.org/graph/enriched` | `data/rdf/enriched.ttl` (Module 3, `klkg-load enriched`) |

A query without `GRAPH` sees the union of all three; the base vs. enriched ablation selects graphs explicitly with `FROM`/`GRAPH`.

## Model

| Namespace | IRI |
|---|---|
| resources | `http://example.org/kl/resource/` (`film/{tmdb}`, `person/{tmdb}`, `credit/{credit_id}`, `genre/`, `keyword/`, `company/`, `collection/`, `country/{iso}`, `language/{iso}`) |
| ontology | `http://example.org/kl/ontology#` |
| SKOS concepts | `http://example.org/kl/concept/` (`occupation/journalist`, ...) |

Films, people, countries and languages link to Wikidata with `owl:sameAs`, and location concepts use `skos:exactMatch`. Direct edges (`schema:actor`, `schema:director`, `kl:writer`) keep multi-hop queries short, while n-ary `kl:CastCredit` and `kl:CrewCredit` nodes keep the character name, billing order and job. The nine plot relations are sub-properties of `kl:plotRelation`, each pointing to its concept scheme through `kl:valueScheme`; the extraction schema and prompt are generated from the vocabulary, so curating `plot_vocabulary.ttl` changes the allowed values without touching code.

## Tests

```bash
.venv/bin/pytest
```

The tests run the whole Module 1 and 2 pipeline on a small synthetic snapshot in `tests/fixtures/`, with no network or GPU.
