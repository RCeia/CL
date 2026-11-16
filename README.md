# Enriching a Movie Knowledge Graph with LLM-Extracted Plot Facts

Knowledge and Language 2026/2027. A base knowledge graph is built from a TMDB snapshot linked to Wikidata, then enriched with plot facts extracted from synopses by an LLM, each annotated with a self-consistency confidence. The base and enriched graphs live side by side in Apache Jena Fuseki as separate named graphs.

| Module | Content | Status |
|---|---|---|
| 1 | TMDB collection, Wikidata alignment, mapping input, throughput pilot | implemented |
| 2 | Ontology, SKOS plot vocabulary, YARRRML/RML mapping, base KG, SHACL, Fuseki | implemented |
| 3 | Plot-fact extraction with 5 samples, confidence, enriched graph | implemented |
| 4 | Annotation sampling, Cohen's kappa, precision/recall, calibration | implemented (needs the manual annotation) |
| 5 | 300-question metadata benchmark with SPARQL-verified gold answers | implemented |

A step-by-step guide in Portuguese, with every command and what each one produces, is in [docs/GUIA_PIPELINE.md](docs/GUIA_PIPELINE.md).

## Code layout

Each pipeline step is a folder in `src/`, numbered in pipeline order; code shared by several steps is in `p0_commons`. Every step file is run directly with the virtual environment's Python, e.g. `.venv/bin/python src/p6_extraction/run_extraction.py`, and finds `config/settings.yaml` from any working directory.

| Folder | Step | Entry point(s) |
|---|---|---|
| `p0_commons` | shared: settings, snapshot reader, SKOS vocabulary, SHACL validation | `validate_shacl.py` (steps 4 and 7) |
| `p1_collect_tmdb` | 1 TMDB snapshot | `collect_movies.py` |
| `p2_align_wikidata` | 2 Wikidata alignment | `wikidata_alignment.py` |
| `p3_prepare_mapping` | 3 mapping input | `prepare_mapping_input.py` |
| `p4_base_kg` | 4 base KG | `build_base_kg.py` |
| `p5_fuseki` | 5 load into Fuseki | `fuseki_loader.py` |
| `p6_extraction` | 6 plot-fact extraction, throughput pilot | `run_extraction.py`, `throughput_pilot.py` |
| `p7_enriched_kg` | 7 enriched KG | `build_enriched_kg.py` |
| `p8_evaluation` | 8 extraction evaluation | `annotation.py`, `extraction_metrics.py` |
| `p9_questions` | 9 template questions | `template_questions.py` |

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
.venv/bin/python src/p1_collect_tmdb/collect_movies.py
.venv/bin/python src/p2_align_wikidata/wikidata_alignment.py
```

`src/p1_collect_tmdb/collect_movies.py` pages through `/discover/movie` sorted by `vote_count.desc` within the configured years, keeps movies with an `en-US` overview, and stores one `/movie/{id}` response per candidate (with credits, keywords and external IDs) under `data/raw/tmdb/`. The candidate list is frozen in `data/raw/tmdb/manifest.json`; rerunning resumes missing downloads, and `--refresh` starts a new snapshot.

`src/p2_align_wikidata/wikidata_alignment.py` resolves Wikidata IDs in batched SPARQL queries: films by P4947 (falling back to TMDB `external_ids`), people by P4985, countries by P297, languages by P218, plus the films' P840 narrative locations for later evaluation. Results are cached in `data/raw/wikidata/` together with `alignment_report.json`.

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
.venv/bin/python src/p6_extraction/throughput_pilot.py
```

The report lands in `reports/pilot/` and the raw samples in `data/extraction/runs/`. On a 24 GB GPU, set `llm.model` to `Qwen/Qwen2.5-7B-Instruct` and serve it in bf16. The client speaks the OpenAI-compatible API with JSON-schema guided decoding, so other servers work as well: for Ollama, set `llm.base_url` to `http://localhost:11434/v1`, `llm.model` to the Ollama tag, and `llm.supports_n` to `false` (one request per sample).

### 3. Mapping input

Set `dataset.final_size` to the N chosen from the pilot (`null` keeps every candidate), then:

```bash
.venv/bin/python src/p3_prepare_mapping/prepare_mapping_input.py
```

This writes nested JSON to `data/interim/`. Every cast, crew, genre, keyword, company, country and language record carries its `movie_id`, because JSONPath cannot reach a parent element. `keyword_frequencies.json` lists the snapshot's keywords by frequency, to seed the curation of the SKOS vocabulary.

### 4. Base KG and validation

```bash
.venv/bin/python src/p4_base_kg/build_base_kg.py
.venv/bin/python src/p0_commons/validate_shacl.py
```

`src/p4_base_kg/build_base_kg.py` regenerates `mappings/base.rml.ttl` from `mappings/base.yarrrml.yml`, runs morph-kgc with `mappings/morph_kgc.ini`, and writes `data/rdf/base.ttl` plus `base_stats.json`. `src/p0_commons/validate_shacl.py` checks the graph against `ontology/shapes/base_shapes.ttl`, writes the report to `reports/shacl/`, and exits with status 1 if any violation is found (warnings, such as a film without a director, do not fail it).

### 5. Fuseki

```bash
docker compose -f fuseki/docker-compose.yml up -d --build
.venv/bin/python src/p5_fuseki/fuseki_loader.py
```

The image downloads Fuseki 6.2.0 from the Apache archive and checks its SHA-512. The TDB2 dataset `kl` has `unionDefaultGraph` enabled and listens only on `127.0.0.1:3030`. Its endpoints are `/kl/sparql` (query), `/kl/update` and `/kl/data` (Graph Store Protocol). `src/p5_fuseki/fuseki_loader.py` replaces these named graphs:

| Graph | Content |
|---|---|
| `http://example.org/graph/ontology` | `kl_ontology.ttl` and `plot_vocabulary.ttl` |
| `http://example.org/graph/base` | `data/rdf/base.ttl` |
| `http://example.org/graph/enriched` | `data/rdf/enriched.ttl` (Module 3, `src/p5_fuseki/fuseki_loader.py enriched`) |

A query without `GRAPH` sees the union of all three; the base vs. enriched ablation selects graphs explicitly with `FROM`/`GRAPH`.

### 6. Plot-fact extraction

```bash
.venv/bin/python src/p6_extraction/run_extraction.py
```

Every overview of the selected movies (`dataset.final_size`, or `--limit N`) is sent to the LLM `llm.samples_per_synopsis` times with the vocabulary-constrained prompt and JSON schema. Raw samples are appended to `data/extraction/runs/samples_<run>.jsonl` as they arrive, so an interrupted run resumes; the run id hashes the model, prompt, vocabulary and sampling settings. A fact (film, relation, concept) gets confidence = samples that contain it / samples requested. Facts go to `data/extraction/facts.json` and a summary with throughput to `reports/extraction/extraction_report.json`; both are also kept per run as `facts_<run>.json` and `extraction_report_<run>.json`, so runs with different models can be compared.

Without a GPU for vLLM, Ollama works through the same client. Set in `.env`:

```bash
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=gemma3:12b
LLM_SUPPORTS_N=false
```

`LLM_MODEL=<tag>` before the command overrides the model for one run. Options: `--prompt config/prompts/extraction_prompt_v2.txt` (stricter instructions: leave relations empty unless the synopsis supports them), `--shuffle` (a different order of the allowed values in each of the 5 samples, so values picked by position stop agreeing and lose confidence) and `--label` (the run's name in comparisons). The default run keeps the same run id, so changing these options never invalidates earlier runs.

### 7. Enriched KG

```bash
.venv/bin/python src/p7_enriched_kg/build_enriched_kg.py
.venv/bin/python src/p0_commons/validate_shacl.py --enriched
.venv/bin/python src/p5_fuseki/fuseki_loader.py enriched
```

Each fact is written as a direct triple (`res:film/27205 kl:protagonistOccupation klc:occupation/spy`) and as a `kl:PlotFact`, a standard RDF reification (`rdf:subject`, `rdf:predicate`, `rdf:object`) carrying `kl:confidence`, `kl:support`, `kl:sampleCount` and `kl:extractionModel`. `enrichment.min_confidence` (default 0, keep all) drops low-confidence facts. `ontology/shapes/enriched_shapes.ttl` checks the reified facts with SHACL-SPARQL: the predicate is a plot relation, the value belongs to its SKOS scheme, the direct triple is asserted and confidence = support / samples.

### 8. Extraction evaluation

```bash
.venv/bin/python src/p8_evaluation/annotation.py
# annotate data/annotation/precision_sheet.csv and recall_sheet.csv
.venv/bin/python src/p8_evaluation/extraction_metrics.py
```

`precision_sheet.csv` holds `evaluation.precision_sample_size` facts, stratified by support level so every confidence level is represented; confidence stays hidden in `precision_key.json`. Annotators write 1 (correct) or 0 (wrong) in `annotator_1` / `annotator_2`, and `final` settles disagreements. `recall_sheet.csv` holds `evaluation.recall_sample_size` overviews x 9 relations, annotated from scratch with values from `allowed_values` (separated by `;`, `-` for none). The script refuses to overwrite existing sheets without `--force`. `src/p8_evaluation/extraction_metrics.py` writes `reports/calibration/extraction_eval.json` (population-weighted precision, per-level precision with Wilson intervals, ECE, precision vs. minimum confidence, recall by threshold, Cohen's kappa) and `reliability_diagram.png`. When `data/annotation/claude/gold_claude.json` exists it also reports how often that annotation agrees with the team's labels (`claude_vs_humans`).

To compare runs without sampling, every film can be annotated once and every run scored against it:

```bash
.venv/bin/python src/p8_evaluation/compare_models.py data/extraction/facts_<run>.json ...
```

The gold lists, per film and relation, the values the synopsis supports: `core` (stated or unavoidable, used for recall) and `acceptable` (defensible implications); a fact is correct when its value is in either. Values a run proposes that the annotator never saw are written to `to_review_<run>.txt`; verdicts go into the gold's `reviewed_additions`, so every model is judged on the same terms. Output: `reports/calibration/model_comparison.{json,md}`, `model_comparison_calibration.png` and per-run fact labels. The current gold, `data/annotation/claude/gold_claude.json`, was written by Claude (an AI model), not by the team.

### 9. Template questions

```bash
.venv/bin/python src/p9_questions/template_questions.py --verify-fuseki
```

Sixteen templates (6 one-hop, 6 two-hop, 4 three-hop) over directors, cast, writers, genres and films; topic entities are films or people with an unambiguous name. Each question keeps its SPARQL query and the gold answers (1 to `benchmark.max_answers` IRIs) it returns on the base graph; `--verify-fuseki` reruns every query on Fuseki. Output: `data/benchmark/template_questions.json` with `benchmark.questions_per_hop` questions per hop count.

## Model

| Namespace | IRI |
|---|---|
| resources | `http://example.org/kl/resource/` (`film/{tmdb}`, `person/{tmdb}`, `credit/{credit_id}`, `genre/`, `keyword/`, `company/`, `collection/`, `country/{iso}`, `language/{iso}`) |
| ontology | `http://example.org/kl/ontology#` |
| SKOS concepts | `http://example.org/kl/concept/` (`occupation/journalist`, ...) |

Films, people, countries and languages link to Wikidata with `owl:sameAs`, and location concepts use `skos:exactMatch`. Direct edges (`schema:actor`, `schema:director`, `kl:writer`) keep multi-hop queries short, while n-ary `kl:CastCredit` and `kl:CrewCredit` nodes keep the character name, billing order and job. The nine plot relations are sub-properties of `kl:plotRelation`, each pointing to its concept scheme through `kl:valueScheme`; the extraction schema and prompt are generated from the vocabulary, so curating `plot_vocabulary.ttl` changes the allowed values without touching code.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests run the whole pipeline on a small synthetic snapshot in `tests/fixtures/`, with a fake LLM and no network or GPU.
