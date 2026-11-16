# Guia completo da pipeline

*Enriching a Movie Knowledge Graph with LLM-Extracted Plot Facts* — Knowledge and Language 2026/27

Este guia explica cada passo do projeto: o que faz, o ficheiro a correr, o que entra e o que sai. Os comandos
estão escritos para Windows (PowerShell ou Git Bash), a partir da pasta do repositório `CL`. Em Linux/macOS troca-se
`.venv/Scripts/python.exe` por `.venv/bin/python`.

## Organização do código

Cada passo é uma pasta em `src/`, numerada pela ordem da pipeline. O código partilhado por vários passos está em
`p0_commons`.

```
src/
  p0_commons/          configuração, leitura do snapshot, vocabulário SKOS, validação SHACL (passos 4 e 7)
  p1_collect_tmdb/     passo 1  collect_movies.py  (+ tmdb_client.py)
  p2_align_wikidata/   passo 2  wikidata_alignment.py
  p3_prepare_mapping/  passo 3  prepare_mapping_input.py
  p4_base_kg/          passo 4  build_base_kg.py
  p5_fuseki/           passo 5  fuseki_loader.py
  p6_extraction/       passo 6  run_extraction.py  (+ llm_client.py, throughput_pilot.py)
  p7_enriched_kg/      passo 7  build_enriched_kg.py
  p8_evaluation/       passo 8  annotation.py, extraction_metrics.py
  p9_questions/        passo 9  template_questions.py
```

Os nomes não podem ser "0 - Commons" ou "1 - Recolher" porque o Python só importa pastas cujo nome é um
identificador válido: sem espaços nem hífenes, e sem começar por um algarismo. Por isso ficam `p0_…` a `p9_…`.

Cada passo corre-se chamando o seu ficheiro com o Python do ambiente virtual:

```bash
.venv/Scripts/python.exe src/p6_extraction/run_extraction.py
```

Para não escrever `.venv/Scripts/python.exe` de cada vez, ativa-se o ambiente uma vez por terminal com
`.venv\Scripts\Activate.ps1` (PowerShell) ou `source .venv/Scripts/activate` (Git Bash), e a partir daí basta
`python src/p6_extraction/run_extraction.py`. No VS Code, escolhendo o Python do `.venv` (canto inferior direito),
o botão ▶ corre o ficheiro aberto. Os ficheiros encontram o `config/settings.yaml` sozinhos, seja qual for a pasta
de onde são chamados.

## Visão geral

```
TMDB (API) ──► 1 recolher ──► 2 alinhar com Wikidata ──► 3 preparar JSON ──► 4 grafo base + SHACL ──► 5 Fuseki
                                                                                     │
sinopses ──► 6 extração com o LLM (5 amostras) ──► 7 grafo enriquecido + SHACL ──────┘ (grafos nomeados)
                         │
                         └──► 8 folhas de anotação ──► (anotação manual) ──► precisão, calibração, recall, κ

grafo base ──► 9 perguntas de template (1–3 saltos) com respostas certas por SPARQL
```

| Passo | Ficheiro(s) a correr | Produz | Precisa de |
|---|---|---|---|
| 1 | `src/p1_collect_tmdb/collect_movies.py` | `data/raw/tmdb/` | chave TMDB |
| 2 | `src/p2_align_wikidata/wikidata_alignment.py` | `data/raw/wikidata/` | internet |
| 3 | `src/p3_prepare_mapping/prepare_mapping_input.py` | `data/interim/` | — |
| 4 | `src/p4_base_kg/build_base_kg.py`, `src/p0_commons/validate_shacl.py` | `data/rdf/base.ttl`, `reports/shacl/` | — |
| 5 | `docker compose …`, `src/p5_fuseki/fuseki_loader.py` | grafos no Fuseki | Docker |
| 6 | `src/p6_extraction/run_extraction.py` | `data/extraction/facts.json`, `reports/extraction/` | LLM (Ollama ou vLLM) |
| 7 | `src/p7_enriched_kg/build_enriched_kg.py`, `src/p0_commons/validate_shacl.py --enriched`, `src/p5_fuseki/fuseki_loader.py enriched` | `data/rdf/enriched.ttl` | passo 6 |
| 8 | `src/p8_evaluation/annotation.py`, anotação, `src/p8_evaluation/extraction_metrics.py` | `data/annotation/`, `reports/calibration/` | vocês os dois |
| 9 | `src/p9_questions/template_questions.py --verify-fuseki` | `data/benchmark/template_questions.json` | passo 4 (e 5 para verificar) |

Os passos 1 a 5 e 9 correm em minutos. O passo 6 é o único demorado. O passo 8 tem uma parte manual.

---

## 0. Preparar o ambiente (uma vez)

```bash
cd "C:/Users/rodri/OneDrive/Desktop/CLProject/CL"
```

**Python.** O ambiente virtual `.venv` já existe. Num computador novo:

```bash
python -m venv .venv
```
```bash
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Isto instala as bibliotecas (rdflib, pyshacl, morph-kgc…) no `.venv`. O `-e` ("modo editável") regista também a
pasta `src/`, para as pastas dos passos se poderem importar umas às outras (por exemplo, o passo 6 usa
`p0_commons.config`). Não copia nem gera nada: o código corre sempre a partir de `src/`.

**`.env`.** Copiar `.env.example` para `.env` e preencher:

| Variável | Para quê |
|---|---|
| `TMDB_API_KEY` | chave v3 ou token v4 do TMDB (passo 1) |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_SUPPORTS_N` | onde está o LLM (passo 6) |
| `WIKIDATA_CONTACT`, `FUSEKI_URL`, … | opcionais; substituem o `config/settings.yaml` |

Todos os outros parâmetros estão em `config/settings.yaml` (tamanho do dataset, temperatura, número de amostras,
tamanho das amostras para anotar, número de perguntas…).

**Docker Desktop** tem de estar aberto para o passo 5.

**LLM.** Há duas formas de servir o modelo; o código fala com as duas da mesma maneira (API compatível com a da
OpenAI):

| | Ollama (este PC, GPU de 4 GB) | vLLM (GPU de 8–24 GB, Linux/WSL) |
|---|---|---|
| Modelo | `gemma3:12b` (o melhor na comparação; o `qwen2.5:3b` é o mais rápido) | `Qwen/Qwen2.5-7B-Instruct(-AWQ)`, o da proposta |
| `.env` | `LLM_BASE_URL=http://localhost:11434/v1`<br>`LLM_MODEL=gemma3:12b`<br>`LLM_SUPPORTS_N=false` | deixar vazio (usa o `settings.yaml`) |
| Arrancar | a aplicação Ollama arranca sozinha com o Windows | `vllm serve …` (ver README, secção 2) |
| Velocidade | ~97 s por filme com o 12B, ~18 s com o 3B (5 amostras) | muito mais rápido (pede as 5 amostras de uma vez) |

Na comparação de modelos (ver "Estado atual", no fim) o `gemma3:12b` foi claramente o melhor, mais do que o 7B da
proposta; é o que está no `.env` e no grafo enriquecido. A proposta fala do Qwen2.5-7B, por isso a troca deve ser
referida na checkpoint. Mudar de modelo cria automaticamente um "run" novo, sem misturar resultados.

**Testes** (sem internet nem GPU, ~10 s):

```bash
.venv/Scripts/python.exe -m pytest
```

---

## 1. Recolher os filmes do TMDB

```bash
.venv/Scripts/python.exe src/p1_collect_tmdb/collect_movies.py --pool-size 100
```

Pede ao TMDB os filmes mais votados entre 1980 e 2025 (`/discover/movie`), guarda a lista em
`data/raw/tmdb/manifest.json` e descarrega para cada filme um JSON com elenco, equipa, keywords e IDs externos.
Correr outra vez só descarrega o que falta; `--refresh` começa um snapshot novo. Sem `--pool-size` usa os 3000 do
`settings.yaml` (~500 MB).

## 2. Ligar ao Wikidata

```bash
.venv/Scripts/python.exe src/p2_align_wikidata/wikidata_alignment.py
```

Encontra o item do Wikidata de cada filme, pessoa, país e língua com consultas SPARQL em lote (propriedades P4947,
P4985, P297, P218), e guarda também os locais da narrativa (P840) para comparar com a extração. Resultado em
`data/raw/wikidata/` e o resumo em `alignment_report.json`. `--refresh` volta a perguntar.

## 3. Preparar o JSON para o mapeamento

```bash
.venv/Scripts/python.exe src/p3_prepare_mapping/prepare_mapping_input.py
```

Transforma os JSON do TMDB em ficheiros simples em `data/interim/` (um por tipo: filmes, pessoas, créditos,
keywords…), cada registo com o `movie_id` do filme. Gera também `keyword_frequencies.json`, as keywords mais comuns,
para afinar o vocabulário SKOS. `--final-size N` fica só com os primeiros N filmes.

## 4. Construir e validar o grafo base

```bash
.venv/Scripts/python.exe src/p4_base_kg/build_base_kg.py
```
```bash
.venv/Scripts/python.exe src/p0_commons/validate_shacl.py
```

O mapeamento declarativo `mappings/base.yarrrml.yml` (YARRRML) é convertido em RML e executado pelo morph-kgc, que
produz `data/rdf/base.ttl` (triplos RDF em Turtle) e `base_stats.json`. O SHACL (`ontology/shapes/base_shapes.ttl`)
verifica a forma do grafo, por exemplo "cada filme tem exatamente um nome e um ID do TMDB". O relatório fica em
`reports/shacl/base_*`, e o comando falha se houver violações.

## 5. Carregar no Fuseki

```bash
docker compose -f fuseki/docker-compose.yml up -d --build
```
```bash
.venv/Scripts/python.exe src/p5_fuseki/fuseki_loader.py
```

Liga o servidor Fuseki (base de dados de grafos) em http://localhost:3030 e carrega dois grafos nomeados:
`http://example.org/graph/ontology` (ontologia + vocabulário SKOS) e `http://example.org/graph/base`. Uma consulta
sem `GRAPH`/`FROM` vê todos juntos. Para desligar: `docker compose -f fuseki/docker-compose.yml stop` (os dados
ficam guardados).

Experimentar em http://localhost:3030 → dataset `kl` → query:

```sparql
PREFIX schema: <http://schema.org/>
SELECT ?titulo WHERE {
  ?nolan schema:name "Christopher Nolan" .
  ?f schema:director ?nolan ; schema:name ?titulo .
}
```

---

## 6. Extrair os factos do enredo com o LLM

```bash
.venv/Scripts/python.exe src/p6_extraction/run_extraction.py --concurrency 2
```

Para cada filme:

1. Constrói o prompt (`config/prompts/extraction_prompt.txt`) com o título, o ano, a sinopse e, para cada uma das
   9 relações, a lista de valores permitidos, tirada do vocabulário SKOS (`ontology/plot_vocabulary.ttl`).
2. Pede ao LLM 5 respostas (`samples_per_synopsis`) com temperatura 0.7. Um JSON schema obriga cada resposta a ser
   um JSON só com valores da lista (no máximo 3 por relação).
3. Guarda as 5 respostas cruas em `data/extraction/runs/samples_<run>.jsonl` logo que chegam. Se o comando for
   interrompido, volta a correr-se e continua onde parou.

No fim junta as respostas: um facto (filme, relação, valor) tem **confiança = nº de respostas que o contêm / 5**
(self-consistency). Exemplo: se 4 das 5 respostas dizem que o protagonista é `spy`, o facto tem confiança 0.8.

| Ficheiro | Conteúdo |
|---|---|
| `data/extraction/facts.json` | todos os factos com `support` (1–5) e `confidence` (0.2–1.0) |
| `reports/extraction/extraction_report.json` | nº de factos por relação e por confiança, % de JSON válido, segundos por filme, horas projetadas para 2000/2500/3000 filmes |

Cada corrida fica também guardada com o seu nome (`facts_<run>.json`, `extraction_report_<run>.json`), para se
poderem comparar modelos. O `facts.json` é sempre o da última corrida, e é o que os passos 7 e 8 leem.

Opções:

| Opção | Para quê |
|---|---|
| `--limit N` | só os primeiros N filmes, para testar |
| `--concurrency N` | pedidos em paralelo (no Ollama deste PC, 2 chega) |
| `--prompt config/prompts/extraction_prompt_v2.txt` | prompt mais exigente: deixar as relações vazias quando a sinopse não as suporta, não adivinhar local, época ou profissão |
| `--shuffle` | baralha a ordem dos valores permitidos em cada uma das 5 respostas (ver abaixo) |
| `--label nome` | o nome da corrida nas comparações |

Para mudar de modelo numa corrida, põe-se a variável antes do comando:

```bash
LLM_MODEL=gemma3:12b .venv/Scripts/python.exe src/p6_extraction/run_extraction.py --concurrency 2
```

**Porquê o `--shuffle`.** Os modelos pequenos, quando a sinopse não diz nada, escolhem os primeiros valores da
lista (Australia, France, Germany para o local; at sea, desert para o ambiente). Como fazem isso nas 5 respostas,
o erro sai com confiança 5/5 e a self-consistency não o apanha. Com a ordem baralhada em cada resposta, esses
palpites deixam de coincidir e a confiança deles cai.

**Piloto de throughput** (opcional): `.venv/Scripts/python.exe src/p6_extraction/throughput_pilot.py` corre a extração nas primeiras 100 sinopses e
projeta o tempo para escolher o N final. O relatório da extração já dá a mesma projeção.

## 7. Construir, validar e carregar o grafo enriquecido

```bash
.venv/Scripts/python.exe src/p7_enriched_kg/build_enriched_kg.py
```
```bash
.venv/Scripts/python.exe src/p0_commons/validate_shacl.py --enriched
```
```bash
.venv/Scripts/python.exe src/p5_fuseki/fuseki_loader.py enriched
```

Cada facto entra no grafo de duas formas:

```text
# (IRIs abreviados para caber; no ficheiro estão completos)
# 1. o triplo direto, para as consultas de vários saltos serem curtas
res:film/27205  kl:protagonistOccupation  klc:occupation/spy .

# 2. um nó kl:PlotFact (reificação RDF padrão) que guarda a confiança do triplo
res:fact/27205/protagonistOccupation/spy  a kl:PlotFact ;
    rdf:subject res:film/27205 ; rdf:predicate kl:protagonistOccupation ; rdf:object klc:occupation/spy ;
    kl:confidence 0.8 ; kl:support 4 ; kl:sampleCount 5 ; kl:extractionModel "qwen2.5:3b" .
```

Um triplo RDF só tem sujeito, predicado e objeto; para lhe "colar" uma confiança usa-se a **reificação**: um nó que
descreve o triplo (`rdf:subject`, `rdf:predicate`, `rdf:object`) e ao qual se juntam propriedades. É o mesmo padrão
dos nós `CastCredit` do grafo base.

`enrichment.min_confidence` no `settings.yaml` (por defeito 0, ou seja, ficam todos) permite deixar de fora os
factos com pouca confiança. Mantêm-se todos para o limiar poder ser variado mais tarde (H3).

O SHACL do grafo enriquecido (`ontology/shapes/enriched_shapes.ttl`) verifica, com regras SHACL-SPARQL, que:
o predicado é uma das 9 relações do enredo; o valor pertence ao esquema SKOS dessa relação (por exemplo, a profissão
não pode ser um tema); o triplo direto existe; e confiança = support / 5.

O grafo vai para `http://example.org/graph/enriched` no Fuseki. Exemplo de consulta que usa a confiança:

```sparql
PREFIX schema: <http://schema.org/>
PREFIX kl: <http://example.org/kl/ontology#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT ?filme ?profissao ?confianca WHERE {
  ?f schema:name ?filme ; kl:protagonistOccupation ?c .
  ?facto rdf:subject ?f ; rdf:predicate kl:protagonistOccupation ; rdf:object ?c ; kl:confidence ?confianca .
  ?c <http://www.w3.org/2004/02/skos/core#prefLabel> ?profissao .
} ORDER BY DESC(?confianca)
```

Para comparar com e sem enriquecimento, escolhe-se o grafo com `FROM <http://example.org/graph/base>` ou
`FROM <http://example.org/graph/enriched>`.

## 8. Avaliar a extração (precisão, calibração, recall, κ)

### 8.1 Gerar as folhas

```bash
.venv/Scripts/python.exe src/p8_evaluation/annotation.py
```

Cria em `data/annotation/`:

| Ficheiro | O que é |
|---|---|
| `precision_sheet.csv` | ~200 factos extraídos para marcar certo/errado |
| `precision_key.json` | a confiança de cada item, **escondida** de quem anota (para não influenciar) |
| `recall_sheet.csv` | 50 sinopses × 9 relações para anotar do zero, sem ver o que o LLM disse |

Os 200 factos são sorteados **por nível de confiança** (o mesmo número de factos com 1/5, 2/5, … 5/5). Assim todos
os níveis têm exemplos suficientes para o diagrama de fiabilidade. O script não apaga folhas que já existem (podem
ter anotações); para as refazer usa-se `--force`.

### 8.2 Anotar (manual, vocês os dois)

Abrir os CSV no Excel, LibreOffice ou Google Sheets (vírgula ou ponto e vírgula funcionam).

**`precision_sheet.csv`**: ler a `overview` e a `statement` (ex. "protagonist occupation: spy") e escrever na coluna
do anotador `1` se a sinopse diz isso (explicitamente ou por implicação direta) ou `0` se não diz. Rodrigo usa
`annotator_1`, Ivan usa `annotator_2`. Quando discordarem, decidem juntos e escrevem o resultado em `final`.
A regra é a mesma que o prompt dá ao LLM: só conta o que a sinopse suporta, não o que se sabe do filme.

**`recall_sheet.csv`**: para cada sinopse e relação, escrever os valores que a sinopse suporta, escolhidos de
`allowed_values` e separados por `;` (ex. `journey; time travel`). Escrever `-` se nenhum valor se aplica. Cada um
anota sozinho, para medir a concordância (κ).

Estimativa: ~1 h para os 200 factos e ~1 h para as 50 sinopses, por pessoa.

### 8.3 Calcular as métricas

```bash
.venv/Scripts/python.exe src/p8_evaluation/extraction_metrics.py
```

Escreve `reports/calibration/extraction_eval.json` e `reports/calibration/reliability_diagram.png`:

| Métrica | Significado |
|---|---|
| `precision_weighted` | % de factos certos no grafo inteiro. Como a amostra foi sorteada por nível, cada nível pesa o que pesa no total de factos |
| `levels` | precisão em cada nível de confiança, com intervalo de Wilson a 95% (barras do diagrama) |
| `ece` | Expected Calibration Error: distância média entre a confiança e a precisão real (0 = perfeito) |
| `precision_at_min_confidence` | precisão e cobertura se só se guardarem os factos com confiança ≥ x (primeira pista para H3) |
| `agreement` | κ de Cohen entre os dois anotadores |
| `recall.by_min_confidence` | das coisas que a sinopse diz, quantas o LLM encontrou, para cada limiar |

Pode correr-se a meio da anotação: usa só as linhas preenchidas. Se existir a anotação do Claude (8.4), o relatório
inclui também `claude_vs_humans`: em quantos factos a anotação do Claude concorda com a vossa, e o κ.

### 8.4 Comparar modelos (anotação completa feita pelo Claude)

```bash
.venv/Scripts/python.exe src/p8_evaluation/compare_models.py data/extraction/facts_<run1>.json data/extraction/facts_<run2>.json
```

Para comparar modelos sem anotar uma amostra de cada um, há uma anotação dos 100 filmes inteiros em
`data/annotation/claude/gold_claude.json`. **Foi feita pelo Claude (um modelo de IA), não por vocês**, e o ficheiro
di-lo. Para cada filme e relação lista os valores que a sinopse suporta:

- `core`: dito na sinopse ou implicação inevitável (conta para o recall);
- `acceptable`: implicação defensável.

Um facto conta como certo se o valor estiver numa das duas listas. Quando um modelo propõe um valor que o anotador
nunca viu, vai para `to_review_<run>.txt`; o veredicto fica em `reviewed_additions`, para todos os modelos serem
julgados da mesma forma. Os rótulos de cada facto ficam em `fact_labels_claude_<run>.csv`.

Saídas em `reports/calibration/`: `model_comparison.md` (a tabela), `model_comparison.json` e
`model_comparison_calibration.png` (precisão por nível de confiança, um traço por modelo).

## 9. Perguntas de template

```bash
.venv/Scripts/python.exe src/p9_questions/template_questions.py --verify-fuseki
```

Gera 300 perguntas (100 de 1 salto, 100 de 2, 100 de 3) a partir de 16 modelos sobre realizadores, atores,
argumentistas, géneros e filmes. Exemplos:

| Saltos | Pergunta | Caminho no grafo |
|---|---|---|
| 1 | Who directed The Avengers? | filme → realizador |
| 2 | Which other films were directed by the director of Batman Begins? | filme → realizador → filmes |
| 3 | Which actors starred in the other films by the director of Guardians of the Galaxy Vol. 2? | filme → realizador → filmes → atores |

Cada pergunta guarda a entidade de partida (só se usam nomes que não se repetem no grafo), a consulta SPARQL e as
respostas certas (entre 1 e 25). `--verify-fuseki` volta a correr cada consulta no Fuseki e confirma que dá o mesmo.
Resultado em `data/benchmark/template_questions.json` e resumo em `reports/benchmark/`.

Estas perguntas só usam metadados: são o **grupo de controlo**. O grafo base e o enriquecido devem acertar o mesmo
nelas.

---

## Estado atual e checkpoint de 16 Nov

Snapshot de 100 filmes. Extração final com `gemma3:12b` no Ollama (run `ce4ad2feffd5`), escolhido na comparação
de modelos da noite de 15 para 16 Nov.

| Entregável da checkpoint | Estado | Números |
|---|---|---|
| Grafo base | feito | 22 855 triplos, 0 violações SHACL, no Fuseki |
| Grafo enriquecido | feito, com `gemma3:12b` | 964 factos para 99 filmes, 8676 triplos, 0 violações SHACL, no Fuseki |
| Precisão da extração | folha pronta (do `gemma3:12b`), **falta anotar** | 200 factos, 40 por nível de confiança |
| Calibração | script e diagrama prontos, **falta anotar** | sai da mesma folha |
| Perguntas de template | feito | 300 perguntas (100 por nº de saltos), 0 diferenças no Fuseki |
| Comparação de modelos | feita, **com a anotação do Claude** | 8 corridas, tabela abaixo |

### Comparação de modelos (anotação do Claude, não humana)

Todos os factos de cada corrida foram avaliados contra a anotação completa dos 100 filmes feita pelo Claude
(secção 8.4). "≥3/5" é a votação por maioria; "recall core" é a fração dos valores claramente ditos nas sinopses que
a corrida encontrou com ≥3/5.

| Corrida | Factos/filme | Precisão (todos) | Precisão (≥3/5) | Recall core (≥3/5) | ECE | Factos 5/5 errados | s/filme |
|---|---|---|---|---|---|---|---|
| qwen2.5:3b | 23.0 | 18% | 28% | 0.60 | 0.36 | 64% | 18 |
| llama3.2:3b | 24.8 | 20% | 31% | 0.62 | 0.29 | 60% | 18 |
| gemma3:4b | 11.8 | 34% | 37% | 0.67 | 0.43 | 57% | 47 |
| qwen2.5:7b | 16.1 | 27% | 34% | 0.70 | 0.39 | 60% | 55 |
| **gemma3:12b** | 9.6 | **44%** | **51%** | **0.76** | 0.33 | **42%** | 97 |
| gemma3:4b + prompt v2 + shuffle | 15.9 | 30% | 43% | 0.67 | **0.22** | 41% | 38 |
| qwen2.5:3b + shuffle | 30.2 | 16% | 28% | 0.55 | 0.27 | 61% | 18 |
| qwen2.5:3b + prompt v2 + shuffle | 29.7 | 16% | 28% | 0.55 | 0.27 | 60% | 18 |

Gráfico: `reports/calibration/model_comparison_calibration.png`. Tabela gerada: `reports/calibration/model_comparison.md`.

**O que mostra**

- **O modelo maior é o melhor:** o `gemma3:12b` é o mais preciso, o que mais encontra e o que menos erra com 5/5.
  É o que alimenta agora o grafo enriquecido e a folha de anotação.
- **Porque é que os modelos pequenos erram com confiança alta:** quando a sinopse não diz nada, escolhem os primeiros
  valores da lista (Australia, France, Germany; at sea, desert). Fazem-no nas 5 respostas, por isso o erro sai com
  5/5 e a self-consistency não o apanha.
- **O `--shuffle` com o prompt v2 corrige isso num modelo que lê o texto:** no `gemma3:4b` o ECE cai de 0.43 para
  0.22 e a precisão por maioria sobe de 37% para 43%, sem perder recall. No `qwen2.5:3b` só melhora a calibração
  (0.36 → 0.27): os palpites ficam espalhados por 1/5 e 2/5, mas continuam a ser palpites.
- **A época é a pior relação** em todos os modelos (22% no `gemma3:12b`): assumem "present day" sem pistas no
  texto. O vocabulário também tem lacunas ("thief", "superhero") que obrigam a escolher o valor mais próximo.
- **Velocidade:** o `gemma3:12b` demora ~97 s por filme neste PC (GPU de 4 GB), ou seja ~54 h para 2000 filmes.
  Para o N final é preciso uma GPU maior (portátil do Ivan ou outra) ou um N menor.

**A anotação do Claude é provisória.** Serve para comparar modelos de forma igual, mas os números da proposta têm de
vir da vossa anotação. Quando anotarem a folha de precisão, o `extraction_metrics.py` diz também quanto o Claude
concorda convosco (`claude_vs_humans`).

**O que falta para a checkpoint:** anotar `precision_sheet.csv` (agora do `gemma3:12b`) e correr
`src/p8_evaluation/extraction_metrics.py`. As folhas não vão para o git (`data/` é ignorado); partilhem o CSV por
outro meio para os dois anotarem.

**Próxima experiência sugerida:** `gemma3:12b` com `--prompt config/prompts/extraction_prompt_v2.txt --shuffle`
(~3 h neste PC). É a combinação que deve dar a melhor precisão e calibração ao mesmo tempo.

## Próximos passos (checkpoint de 3 Dez)

1. 150 perguntas mistas (metadados + enredo) ancoradas numa entidade, com respostas certas anotadas à mão.
2. Baselines B0 (só o LLM), B1 (BM25 + embeddings sobre um documento por filme) e B2 (o LLM escreve SPARQL).
3. Sistemas S1 (o LLM planeia o caminho e o SPARQL executa) e S2 (agente estilo Plan-on-Graph), nos dois grafos.
4. Métricas: Hits@1, F1, faithfulness, taxa de alucinação, chamadas ao LLM, latência, bootstrap emparelhado (H1, H2).
5. S3 (GCR) e o ranking por confiança com abstenção num subconjunto (primeiros resultados de H3).

---

## Problemas comuns

| Sintoma | Solução |
|---|---|
| `Model gemma3:12b not served` | abrir a aplicação Ollama; `ollama pull gemma3:12b` se o modelo não estiver descarregado |
| `src/p6_extraction/run_extraction.py` muito lento | normal neste PC (~97 s por filme com o 12B); usar `--limit` para testes |
| Docker Desktop não arranca (erro do `engine.sock`) | fechar o Docker Desktop, renomear `%LOCALAPPDATA%\Docker\run` e `%LOCALAPPDATA%\docker-secrets-engine` e abrir de novo |
| `src/p5_fuseki/fuseki_loader.py` falha com "connection refused" | o Fuseki não está ligado: correr o `docker compose … up -d` |
| `src/p8_evaluation/annotation.py` recusa | já existem folhas (podem ter anotações); `--force` só se quiserem mesmo apagá-las |
| `ModuleNotFoundError: No module named 'p0_commons'` (por exemplo, depois de mudar a pasta de sítio) | reinstalar: `.venv/Scripts/python.exe -m pip install -e ".[dev]"` |

## Onde está cada coisa

| Pasta | Conteúdo |
|---|---|
| `config/` | `settings.yaml` (todos os parâmetros) e o prompt da extração |
| `ontology/` | ontologia OWL, vocabulário SKOS e shapes SHACL |
| `mappings/` | mapeamento YARRRML → RML do grafo base |
| `src/p0_commons/` | código partilhado (configuração, snapshot, vocabulário, SHACL) |
| `src/p1_…` a `src/p9_…` | um passo cada (ver "Organização do código" no início) |
| `fuseki/` | Dockerfile e configuração do servidor Fuseki |
| `data/` | tudo o que é gerado (não vai para o git) |
| `reports/` | relatórios em JSON e o diagrama (não vão para o git) |
| `tests/` | testes automáticos com um snapshot sintético e um LLM falso |
