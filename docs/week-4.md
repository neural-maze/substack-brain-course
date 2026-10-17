# Week 4 — Structured claim extraction, graph ontology design, and three-way retrieval fusion

**Course framing:** *Move beyond flat bags of text passages by designing a strict, closed knowledge-graph ontology in Memgraph, enforcing a deterministic verbatim quote guardrail before persistence, fusing graph traversal into three-way Reciprocal Rank Fusion, and exposing full claim lineage via `kb_sources`.*

This guide builds directly on Week 3. Whether you are continuing from an existing database or starting fresh (`make nuke` + `make start`), every command below runs against this repo state.

The main [README.md](../README.md) has the high-level course overview. This file is the full walkthrough.

## What you're building

Through Week 3, the retrieval engine treated newsletters as flat bags of chunked passages. Two-way Reciprocal Rank Fusion (BM25 + `pgvector`) excels at finding passages that match a query's vocabulary or embedding neighborhood, but cannot natively answer relational or structural questions across publications:

- *"Which authors disagree on whether agentic RAG requires a reranker?"*
- *"How have claims about test-time compute evolved across Ahead of AI, Decoding AI, and The Neural Maze?"*
- *"What concrete measurements or predictions has a specific author made about small language models?"*

By the end of this guide, you will have:

- **A locked 5-node, 5-edge graph ontology (`kb/graph/ontology.py`)**: `:Publication`, `:Author`, `:Article`, `:Claim`, and `:Topic` nodes connected by `[:PUBLISHED_IN]`, `[:WRITTEN_BY]`, `[:ASSERTS]`, `[:ABOUT]`, and `[:SUPERSEDES]`, with deterministic topic canonicalization and a stop-topic blocklist.
- **A hand-written agent execution layer (`kb/agents/`)**: ~200 lines of transparent Python built directly on `ctx.step.run`, with zero agent frameworks.
- **The First Free Guardrail (`kb/sources/quote_validator.py`)**: Deterministic Python verification that every LLM-extracted claim carries an exact verbatim `quote_span` from the source article before it ever touches Postgres or Memgraph.
- **Durable enrichment (`extract-claims` in `kb/functions/enrich.py`)**: Triggered by `kb/article.ingested` and `kb/article.updated`, persisting claims to Postgres (`claims` table) and idempotently merging the attribution subgraph into Memgraph.
- **Three-way hybrid retrieval (`kb/retrieval/graph.py` & `kb/retrieval/hybrid.py`)**: Fuses BM25, `pgvector`, and 2-hop Memgraph claim traversal via Reciprocal Rank Fusion (`hybrid_3way_rrf_k60`) and surfaces cross-publication disagreements in `POST /ask` and `kb_ask`.
- **Provenance inspection (`kb_sources` & `GET /sources/{id}`)**: The 9th MCP tool, resolving any cited `claim_id` or `passage_id` to its verbatim quote, author, publication, epistemic `claim_type`, and temporal validity (`valid_from`, `valid_to`, `supersedes_id`).

```
kb/article.ingested ──┐
kb/article.updated  ──┴──▶ extract-claims (6 durable steps)
                             ├─ 1. load-article (Postgres)
                             ├─ 2. extract-claims-llm (Claude via run_agent)
                             ├─ 3. validate-quote-spans (verbatim check + topic canonicalization)
                             ├─ 4. persist-claims-pg (supersede old + upsert claims)
                             ├─ 5. sync-claims-memgraph (idempotent MERGE subgraph)
                             └─ 6. emit-claims-extracted (kb/claims.extracted)

POST /ask & kb_ask ──▶ BM25 + pgvector + Memgraph (3-way RRF) ──▶ Claude (surfaces disagreements) ──▶ snapshots
kb_sources(id)     ──▶ Fast read (<1s): full claim/passage lineage, verbatim quote, valid_from / valid_to
```

---

## Prerequisite Zero: Why ontology comes before code

Most LLM-backed knowledge graphs fail in production because teams jump straight to prompting an LLM to *"extract entities and relationships"* without a bounded ontology. Unconstrained extraction produces four failure modes:

1. **Unbounded schema drift**: The LLM invents arbitrary node labels (`:Framework`, `:Benchmark`) and edge types (`:USES`, `:CRITICIZES`), making deterministic Cypher impossible.
2. **Topic synonym fragmentation**: `"RAG"`, `"rag"`, and `"Retrieval-Augmented Generation"` land on disconnected nodes, breaking cross-publication traversal.
3. **Hallucinated attribution**: Paraphrasing a nuanced statement into an absolute claim without a verifiable link to the exact source words.
4. **Write-time interpretation bias (`:CONTRADICTS` edges)**: Deciding whether Claim A contradicts Claim B at ingestion time requires $O(N^2)$ comparisons and freezes brittle judgments into permanent edges.

Our ontology (`kb/graph/ontology.py`) prevents all four:

- **PostgreSQL owns prose and vectors** (`articles.text`, `passages.text`, `tsvector`, `pgvector`). **Memgraph owns attribution topology** (`:Publication`, `:Author`, `:Article`, `:Claim`, `:Topic`). Full passage text never enters Memgraph.
- Claims connect neutrally through canonical `:Topic` hubs (`(:Claim)-[:ABOUT]->(:Topic)`), deferring disagreement synthesis to query time (`POST /ask`).
- Every `:Claim` carries an epistemic `claim_type` (`"opinion"`, `"measurement"`, or `"prediction"`) and a verbatim `quote_span`.

---

## Step 1 — Start the stack (Postgres + Inngest + Memgraph + Memgraph Lab)

Run:

```bash
make start
```

In Week 4, `make start` launches two additional containers in `infra/docker-compose.yml`:
- **Memgraph (`localhost:7687`)**: In-memory Bolt graph database backed by the `kb_memgraph_data` volume.
- **Memgraph Lab (`http://localhost:3000`)**: Visual graph explorer for running Cypher queries and inspecting the ontology.

It also runs `alembic upgrade head`, applying `alembic/versions/f5b8c3d4e6f7_week_4_claims_table.py` to create the `claims` table and its partial index `ix_claims_article_active WHERE valid_to IS NULL`.

Check health across all three subsystems:

```bash
curl http://localhost:8000/health
```

You should see `"database": "ok"`, `"inngest": "ok"`, and `"memgraph": "ok"`, along with `"articles"`, `"passages"`, `"claims"`, and `"topics"` counts.

---

## Step 2 — Trigger claim extraction and verify idempotency

Whenever `ingest-article` finishes ingesting a new article (`kb/article.ingested`) or updating an edited one (`kb/article.updated`), Inngest automatically triggers `extract-claims` (`kb/functions/enrich.py`).

Ingest an article (or trigger a backfill):

```bash
curl -X POST http://localhost:8000/articles \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.theneuralmaze.com/p/the-complete-guide-to-event-driven"}'
```

Open the Inngest Dev Server UI at `http://localhost:8288`:
1. `ingest-article` runs and emits `kb/article.ingested`.
2. `extract-claims` starts automatically and executes its six durable steps:
   - `load-article`
   - `extract-claims-llm`
   - `validate-quote-spans`
   - `persist-claims-pg`
   - `sync-claims-memgraph`
   - `emit-claims-extracted`

### Verify the quote-span guardrail and Postgres `claims` table

```bash
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U substack_brain -d substack_brain -c \
  "SELECT id, claim_type, confidence, LEFT(text, 60) AS claim, LEFT(quote_span, 60) AS verbatim_quote FROM claims WHERE valid_to IS NULL LIMIT 10;"
```

Every stored claim has a deterministic UUIDv5 (`kb/schemas/ids.py:claim_id`), so re-running the same `kb/article.ingested` event in Inngest updates in place and merges on `{claim_id}` in Memgraph—creating **zero duplicate rows, zero duplicate nodes, and zero duplicate edges**.

---

## Step 3 — Explore the graph in Memgraph Lab (`localhost:3000`)

Open `http://localhost:3000` in your browser (Memgraph Lab connects automatically to `memgraph:7687`).

Run the cross-publication traversal query in the Query Execution tab:

```cypher
// Traverse: Author 1 -> Article -> Claim -> Shared Topic <- Claim <- Article <- Author 2
MATCH (a1:Author)<-[:WRITTEN_BY]-(art1:Article)-[:ASSERTS]->(c1:Claim)-[:ABOUT]->(t:Topic)<-[:ABOUT]-(c2:Claim)<-[:ASSERTS]-(art2:Article)-[:WRITTEN_BY]->(a2:Author)
WHERE c1.valid_to IS NULL
  AND c2.valid_to IS NULL
  AND a1.slug <> a2.slug
RETURN a1, art1, c1, t, c2, art2, a2
LIMIT 25;
```

Because `kb/graph/ontology.py` normalizes surface variations (`"RAG"`, `"rag"`, `"Retrieval-Augmented Generation"` $\rightarrow$ `:Topic {slug: "rag"}`), claims from different newsletters converge on shared `:Topic` hubs.

---

## Step 4 — Three-way hybrid retrieval and cross-publication synthesis

Ask a question that spans multiple newsletters:

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "How should engineers design RAG retrieval and agent orchestration in production?"}'
```

Under the hood:
1. `kb/retrieval/hybrid.py` runs `search_passages_bm25` (lexical), `search_passages_dense` (pgvector cosine), and `search_graph_claims` (2-hop Memgraph `:Topic` $\rightarrow$ `:Claim` traversal), fusing all three rankings with Reciprocal Rank Fusion (`retriever_variant = "hybrid_3way_rrf_k60"`).
2. `kb/functions/ask.py` formats each passage alongside any attributed claim and epistemic `claim_type` (`opinion`, `measurement`, `prediction`), instructing Claude to surface genuine disagreements across authors with inline citations.
3. Inspect the latest retrieval snapshot in Postgres:

```bash
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U substack_brain -d substack_brain -c \
  "SELECT snapshot_id, retriever_variant, jsonb_pretty(retrieved) FROM snapshots ORDER BY created_at DESC LIMIT 1;"
```

Each candidate in `snapshots.retrieved` records `sparse_rank`, `dense_rank`, `graph_rank`, `claim_id`, `claim_type`, and `source` (`"bm25"`, `"vector"`, `"both"`, `"graph"`, `"bm25+graph"`, `"vector+graph"`, or `"all"`).

---

## Step 5 — Verify provenance with `kb_sources`

Take any `claim_id` or `passage_id` from a `/ask` citation and query its full provenance via HTTP or the `kb_sources` MCP tool:

```bash
curl http://localhost:8000/sources/<claim_or_passage_id>
```

It returns:
- `kind`: `"claim"` or `"passage"`
- `article`: `{article_id, canonical_id, title, url, published_at, content_hash}`
- `publication`: `{slug, name, homepage}`
- `author`: author name
- `verbatim_quote`: the exact substring from the article (`quote_span` or passage text)
- `claim_text`, `claim_type`, `confidence`, `topics` (when `kind == "claim"`)
- `validity`: `{valid_from, valid_to, is_active, supersedes_id}`

---

## Step 6 — Run the test suite

Run the unit tests (no Docker or API keys needed):

```bash
uv run python -m pytest tests/unit/test_ontology.py tests/unit/test_quote_span.py tests/unit/test_rrf_three_way.py tests/test_week4.py
```

With the local stack running (`make start`), run the full lint, typecheck, and integration test suite:

```bash
make check
```

## Verification checklist

- `make check` passes (`ruff check .`, `mypy`, `pytest`).
- `GET /health` and `kb_health` report `"memgraph": "ok"`, `"claims"`, and `"topics"`.
- `extract-claims` triggers on `kb/article.ingested` and `kb/article.updated`, drops non-verbatim quotes, writes to Postgres `claims`, and merges `:Publication`, `:Author`, `:Article`, `:Claim`, `:Topic` into Memgraph with zero duplicates on replay.
- Editing an article sets `valid_to` on prior active claims and merges `[:SUPERSEDES]` edges in Memgraph.
- `POST /ask` and `kb_ask` record `retriever_variant = "hybrid_3way_rrf_k60"` with graph-ranked candidates in `snapshots.retrieved`.
- `kb_sources` (the 9th MCP tool) resolves both `claim_id` and `passage_id` to their verbatim quote and temporal validity metadata.
