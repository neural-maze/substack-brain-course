---
title: "The Living Knowledge Base: HTTP Conditional Freshness, Temporal Supersession, Failure Datasets, and the Model Context Protocol (MCP)"
subtitle: "Lesson 3 / 6: A Masterclass on Being a Good Citizen of the Web, Non-Destructive Content Drift, and Giving Agents a Standardized Interface"
authors:
  - name: "Miguel Otero Pedrido"
    url: "https://substack.com/@migueloteropedrido"
    role: "ML / AI Engineer | Founder @ The Neural Maze"
  - name: "Antonio Zarauz Moreno"
    url: "https://substack.com/@antoniozarauzmoreno"
    role: "Mathematician"
  date: "2026-10-14"
original_url: "https://theneuralmaze.substack.com/p/week-3-freshness-temporal-supersession-mcp"
publication: "The Neural Maze"
tags:
  - Inngest
  - Durable Execution
  - Model Context Protocol
  - MCP
  - Temporal Validity
  - HTTP Conditional Requests
  - PostgreSQL
  - pgvector
  - Anthropic Claude
  - Python
---

# The Living Knowledge Base: HTTP Conditional Freshness, Temporal Supersession, Failure Datasets, and the Model Context Protocol (MCP)

### *Lesson 3 / 6: A Masterclass on Being a Good Citizen of the Web, Non-Destructive Content Drift, and Giving Agents a Standardized Interface*

**By Miguel Otero Pedrido and Antonio Zarauz Moreno**  
*Published on October 14, 2026*

---

Hey friends! 👋

Welcome to **Lesson 3** of our 6-week curriculum on **Harness Engineering**. 

Over the previous two weeks, we laid down the foundational scaffolding of an industrial-strength knowledge engine. In Lesson 1, we tackled process crashes and non-deterministic execution with Inngest’s durable step memoization. In Lesson 2, we confronted the physics of distributed networks, enforcing strict flow control per publication origin, resolving lexical and vector search disagreements with Reciprocal Rank Fusion (RRF), and preserving verifiable audit trails via the Context Snapshot pattern.

Yet, every pipeline we have built so far shared a quiet, unrealistic assumption: that published content is static.

In production, technical writing is alive. Authors edit breaking analyses to correct erroneous code snippets. Newsletter writers publish addendums, revise benchmarks, or fix typos hours after an initial dispatch. When your pipeline blindly ingests the web on a fixed polling schedule, two disastrous failure modes emerge:

1. **Destructive Poll Swarms**: A naive background cron worker aggressively downloads and parses complete RSS feeds every few minutes. Ninety-nine percent of the time, not a single article has changed. You waste network bandwidth, burn CPU cycles re-parsing megabytes of XML, and abuse the hosting infrastructure of the very creators you depend on until their rate limiters ban your scraper.
2. **The Update Dilemma**: When an article is edited, naive databases either execute an in-place SQL `UPDATE`—erasing the historical text and permanently invalidating prior citations, snapshots, and evaluations—or they insert a duplicate article row, causing vector search to retrieve competing, contradictory versions of the exact same passage.

Today, we resolve both dilemmas while exposing our system to external AI agents. We transform our knowledge base into a living, good-citizen subscriber of external feeds, implement non-destructive temporal supersession in PostgreSQL, turn production runtime failures into an automated evaluation dataset, and wrap every capability built to date into an official **Model Context Protocol (MCP)** server.

---

> 🎥 **VIDEO 1: The Architectural Blueprint — Living Feeds, Temporal Validity & The MCP Interface**  
> *(Video duration: ~6–8 mins)*  
> **What to record on screen:**  
> 1. **The Core Philosophy**: Walk through the architectural diagram contrasting naive cron scrapers against HTTP conditional polling (`ETag` / `If-None-Match`). Highlight why a `304 Not Modified` response that terminates in under 15 milliseconds is the gold standard of pipeline citizenship.  
> 2. **The Temporal Reality**: Open PostgreSQL and illustrate the schema invariants of `valid_from`, `valid_to`, and partial indexing. Explain why destructive `DELETE` statements or blind `UPDATE` overwrites break RAG reproducibility and LLM auditability.  
> 3. **The Agent Surface**: Demonstrate why the Model Context Protocol (MCP) requires no new orchestration contracts. Show how fast reads bypass Inngest to hit PostgreSQL directly, while mutation tools dispatch the exact same event schemas established in Weeks 1 and 2.

---

# 1. The Physics of Freshness: How to Be a Good Citizen of the Web

Every software engineer building data pipelines eventually faces the temptation to poll. When third-party platforms do not offer outbound webhooks, the only mechanism to discover new publications is periodic inspection.

The naive approach is devastatingly simple and equally reckless: configure a scheduled task to run every five minutes, download the origin feed, parse the full payload, and compare IDs against storage. At five-minute intervals across dozens of publications, this brute-force approach hammers origin servers with hundreds of redundant HTTP requests daily. It consumes server egress, burns local compute, and flags your IP address as an abusive bot.

High-performance web architecture solved this problem decades ago through RFC 9110 HTTP Conditional Requests:

![Conditional HTTP Freshness Cycle](images/http_conditional_freshness.svg)

When our freshness function (`poll-feeds`) checks an origin feed, it does not issue a blank `GET` request. It inspects our local `cursors` table in PostgreSQL, extracting the cryptographic validator (`ETag`) and timestamp (`Last-Modified`) received during the last successful poll. It transmits these values via the standard HTTP headers `If-None-Match` and `If-Modified-Since`.

If the publication has not posted an article, the remote server evaluates the validators at its edge and immediately responds with `HTTP 304 Not Modified`. The response body is completely empty.

Our pipeline parses zero XML, allocates zero article objects, dispatches zero downstream events, and updates only the cursor's diagnostic counter. The Inngest execution step completes in single-digit milliseconds, costing virtually zero operational spend.

Only when an editorial change or new post occurs does the remote server transmit `HTTP 200 OK` with the new body. We update the stored cursor, extract the feed entries, compute their canonical deduplication identifiers, and dispatch `kb/article.discovered` exclusively for the newly detected items.

```python
# kb/sources/rss.py (Surgical Conditional Retrieval)
async def fetch_feed_conditional(
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> tuple[int, bytes | None, str | None, str | None]:
    headers = {"User-Agent": settings.http_user_agent}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 304:
            return 304, None, etag, last_modified
        return (
            resp.status_code,
            resp.content,
            resp.headers.get("ETag"),
            resp.headers.get("Last-Modified"),
        )
```

By decoupling polling frequency from computational load, we achieve near-instantaneous discovery of breaking articles while remaining polite, unobtrusive consumers of publishing infrastructure.

---

# 2. Content Drift and Non-Destructive Temporal Supersession

What happens when an author edits an existing article?

Suppose Miguel writes an analysis of multi-agent orchestration containing a broken code block. Three hours after publication, he edits the post, correcting the implementation. On the subsequent freshness poll, the origin server returns `HTTP 200` because the feed content changed.

In a naive RAG architecture, one of two catastrophic design flaws manifests:

1. **The Destructive Overwrite**: The pipeline runs an unconditional `UPDATE` on the `articles` and `passages` tables. The previous text is obliterated. While this appears clean at first glance, it completely shatters auditability. Any Context Snapshot recorded during those intervening three hours now cites passage IDs that point to text the model never actually saw. Evaluations measuring hallucination rates become impossible to reproduce because the underlying ground truth was destroyed.
2. **The Zombie Duplication**: If the system generates fresh passage UUIDs without cleaning up previous entries, vector search begins returning both the old, flawed snippet and the new, corrected snippet. Reciprocal Rank Fusion ranks both highly due to lexical overlap, and the downstream LLM synthesizes an incoherent answer that cites contradictory assertions from the same author.

To solve this permanently, we apply the **Temporal Validity Pattern**:

![Temporal Supersession: Non-Destructive Content Drift](images/temporal_supersession.svg)

In our database design, active passages are defined not by their physical presence on disk, but by a temporal predicate: `valid_to IS NULL`.

When an article event arrives at `ingest-article`, we retrieve the existing record by its immutable `canonical_id` and evaluate its SHA-256 `content_hash`. If the hash matches, the step short-circuits immediately with zero OpenAI embedding calls. 

If the hash has changed, the article was edited. We fetch and parse the revised text, generate new chunks, and calculate fresh embeddings. But before persisting the revisions, we execute an atomic supersession query:

```python
# kb/db/queries.py (Atomic Non-Destructive Supersession)
async def supersede_passages(session: AsyncSession, article_id: uuid.UUID) -> int:
    """Close active passages by setting valid_to = now(). History is preserved."""
    stmt = (
        update(Passage)
        .where(Passage.article_id == article_id, Passage.valid_to.is_(None))
        .values(valid_to=func.now())
    )
    result = await session.execute(stmt)
    return result.rowcount
```

Instead of executing a destructive SQL `DELETE`, we mark existing passages as historical artifacts by sealing their `valid_to` timestamp. Immediately following supersession, we insert the newly chunked passages with their `valid_to` initialized to `NULL`.

To support this cleanly under PostgreSQL uniqueness rules, we adjust the table constraints. A traditional unique index on `(article_id, chunk_index)` would prevent inserting chunk 0 of the new version while chunk 0 of the old version still exists. 

We resolve this by transforming the constraint into a **partial unique index**:

```sql
CREATE UNIQUE INDEX uq_passages_article_chunk 
ON passages (article_id, chunk_index) 
WHERE valid_to IS NULL;
```

This ensures that at any given moment, exactly one active version of a passage chunk can exist for an article, while an arbitrary number of historical revisions remain preserved in storage. 

Our retrieval queries—both the BM25 full-text ranker and the pgvector HNSW index—filter exclusively on `valid_to IS NULL`. The active search path remains blazing fast and free of contradictions, while our historical evaluations retain 100% bitwise reproducibility.

---

# 3. Turning Failures into an Operational Dataset

In production systems, failures are typically treated as disposable waste. A process crashes, a stack trace is dumped into a cloud log aggregator, and the system retries until the error is either resolved or silenced.

In Harness Engineering, **failures are treated as high-value training data**.

When an upstream article fails to parse due to malformed HTML, or an embedding provider returns an unexpected schema, discarding that error represents an unrecoverable loss. Those runtime edge cases constitute the exact evaluation dataset required to benchmark the robustness of our pipeline in Week 5.

Inngest provides a native, zero-infrastructure bridge to capture these incidents: the `inngest/function.failed` system event.

Whenever any function registered with our Inngest client exhausts its retry budget or raises an uncaught non-retriable exception, the Inngest engine synthesizes an `inngest/function.failed` event containing the failed function's identifier, the execution run ID, the original triggering event payload, and the serialized error message.

We capture these events with a dedicated operational function (`handle-function-failed` in `kb/functions/ops.py`), recording them directly into an operational `failures` table:

```python
# kb/functions/ops.py (Production Failure Recorder)
@client.create_function(
    fn_id="handle-function-failed",
    name="Record Function Failures",
    trigger=inngest.TriggerEvent(event="inngest/function.failed"),
)
async def handle_function_failed(ctx: inngest.Context) -> dict[str, object]:
    data = ctx.event.data or {}
    function_id = str(data.get("function_id") or ctx.event.name)
    run_id = str(data.get("run_id") or ctx.event.id)
    raw_error = data.get("error")
    error_msg = json.dumps(raw_error) if isinstance(raw_error, dict) else str(raw_error)

    async with get_session() as session:
        failure_id = await record_failure(
            session,
            function_id=function_id,
            run_id=run_id,
            error=error_msg,
            event_data=data.get("event"),
        )
        await session.commit()

    return {"failure_id": str(failure_id), "status": "recorded"}
```

By deploying this recorder in Week 3, our system begins passively accumulating genuine production failure modes during normal development and staging. When we construct our automated evaluation suite two weeks from now, we will not need to fabricate synthetic benchmark errors—we will replay actual real-world anomalies captured from the wild.

---

# 4. The Model Context Protocol (MCP) Interface

With our storage, retrieval, freshness, and failure layers hardened, we arrive at the central milestone of Lesson 3: exposing our knowledge base to AI developer environments.

Whether you operate in **Claude Code**, **Cursor**, **Google Antigravity**, or an autonomous agent framework, AI assistants require structured, deterministic interfaces to interact with internal tools. The industry has converged on Anthropic's **Model Context Protocol (MCP)** as the standard contract for tool discovery, execution, and sampling.

However, integrating an MCP server into an existing backend often leads teams into architectural disarray. Developers frequently attempt to reimplement pipeline logic directly inside tool handlers or introduce nested orchestration layers that create latency bottlenecks.

Our implementation adheres strictly to **The Asymmetry Principle**, organizing all eight tools into two distinct execution tiers:

![The 8 Week-3 MCP Tools: The Asymmetry Principle](images/mcp_tools_asymmetry.svg)

Notice the decisive architectural boundary:

### Fast Reads Bypass Inngest Completely
Tools designed for consumption—`kb_health`, `kb_search`, `kb_ask`, `kb_list_publications`, and `kb_job_status`—execute directly against PostgreSQL and the retrieval engines. When an agent invokes `kb_search("agentic memory architectures")`, it does not want to wait for an asynchronous job to be queued and scheduled. It demands immediate, high-precision context in under 800 milliseconds. 

Similarly, `kb_ask` runs our hybrid retrieval, generates inline citations using Claude, writes an immutable context snapshot to the database, and returns the grounded synthesis immediately, emitting its telemetry event asynchronously on the way out.

### Slow Writes Dispatch Events and Return Immediately
Tools designed to trigger ingestion—`kb_add_publication`, `kb_backfill`, and `kb_sync`—never execute long-running scraping or embedding loops synchronously inside the MCP process. Doing so would freeze the stdio transport, trip tool-calling timeouts in Claude Code or Cursor, and leave the agent hanging indefinitely.

Instead, mutation tools act as ultra-thin event dispatchers. When an agent calls `kb_backfill(publication="the-neural-maze")`, the tool constructs an Inngest event, transmits it to the local Dev Server, and returns a structured handle within 30 milliseconds:

```json
{
  "event_id": "01JMZ123ABC456DEF789",
  "trace_url": "http://localhost:8288/event/01JMZ123ABC456DEF789",
  "status": "queued"
}
```

The agent is immediately unblocked. It can continue writing code, inspect the live execution graph in the Inngest UI via the returned `trace_url`, or periodically poll `kb_job_status(event_id)` to monitor progress.

Crucially, **the MCP layer adds zero new orchestration logic**. Every tool is a concise, typed wrapper around functions and events that already existed in Weeks 1 and 2. The contract remains pristine.

---

# 5. Local Deployment & Operational Walkthrough

Here is your exact, reproducible guide to booting the stack, verifying conditional freshness, testing temporal supersession, and connecting your local Claude Code or Cursor client over MCP.

### Step 1: Initialize the Infrastructure and Apply Migrations

Ensure your local `.env` configuration is populated, then launch the PostgreSQL 16 container, pgvector extensions, and Inngest Dev Server:

```bash
cd articles/inngest-series/20261014/deployment
make start
```

With Docker running, apply the database migrations to provision the `snapshots`, `cursors`, and `failures` tables along with the partial supersession index:

```bash
uv run alembic upgrade head
```

Verify that the test suite passes, confirming that all unit contracts and schemas align:

```bash
uv run python -m pytest
```

### Step 2: Launch the Backend Service

In your primary terminal, start the FastAPI application that serves our Inngest functions:

```bash
make serve
# Running on http://localhost:8000
```

Open the Inngest Dev Server dashboard at `http://localhost:8288`. You will see all four registered functions:
- `backfill-publication` (Listens to `kb/publication.added`)
- `ingest-article` (Listens to `kb/article.discovered`)
- `poll-feeds` (Cron `*/5 * * * *` + Event `kb/feed.poll`)
- `handle-function-failed` (Listens to `inngest/function.failed`)

---

> 🎥 **VIDEO 2: The Live Operational Demo — Conditional 304s, Supersession & Live Agent Interaction**  
> *(Video duration: ~8–10 mins)*  
> **What to record on screen:**  
> 1. **Zero-Compute 304 Polling**: Trigger `kb/feed.poll` manually from terminal or the Inngest UI. Watch `poll-feeds` complete across all publications in under 20 milliseconds, logging `status: 304` and creating zero downstream article runs.  
> 2. **Temporal Supersession in Action**: Seed or edit an article locally. Trigger ingestion and query PostgreSQL directly: show the original passages receiving a `valid_to` timestamp while the revised passages appear with `valid_to = NULL`. Run `kb_search` to prove only the revised text is retrieved.  
> 3. **The Live MCP Agent Session**: Configure Claude Code, Cursor, or Google Antigravity to connect to `kb-mcp`. In natural language, ask the agent to check KB health, trigger a sync, search for specific technical topics, and ask grounded questions with inline citations.

---

### Step 3: Verifying Conditional Polling (The 304 Fast Path)

To witness HTTP conditional polling in action, dispatch a poll event via `curl`:

```bash
curl -X POST http://localhost:8288/e/kb/feed.poll -H "Content-Type: application/json" -d '{}'
```

Inspect the run in the Inngest dashboard. You will observe `poll-feeds` executing across every publication listed in `publications.yaml`. For each feed, the origin responds with HTTP 304. The entire run finishes in milliseconds, emitting zero `kb/article.discovered` events.

### Step 4: Testing Non-Destructive Supersession

To verify that content updates do not cause duplicates or destructive deletions, run our verification test:

```bash
uv run python -m pytest tests/test_week3.py -k "supersession or updated"
```

In the database, query the `passages` table for an updated article:

```sql
SELECT id, chunk_index, valid_from, valid_to, LEFT(text, 50) AS snippet 
FROM passages 
ORDER BY chunk_index, valid_from;
```

You will see the old versions preserved with their historical `valid_to` timestamps intact, while the active versions retain `valid_to = NULL`. A hybrid search query targets only active rows:

```bash
curl "http://localhost:8000/search?q=neural+maze&limit=3" | jq .
```

### Step 5: Connecting the MCP Server to Claude Code, Cursor & Antigravity

We installed our CLI entrypoint `kb-mcp` directly into the project environment. To expose the living knowledge base to your AI developer environment, register the server in your configuration.

#### 1. Claude Code
In `~/.claude.json` (global) or `.claude.json` (workspace):

```json
{
  "mcpServers": {
    "living-kb": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/hedrergudene/Documents/GitHub/tnm-articles/articles/inngest-series/20261014/deployment",
        "run",
        "kb-mcp"
      ]
    }
  }
}
```

#### 2. Cursor
In `cursor_settings.json` (or **Cursor Settings** -> **Features** -> **MCP** -> **+ Add New MCP Server**):
- **Name**: `living-kb`
- **Type**: `command`
- **Command**: `uv --directory /Users/hedrergudene/Documents/GitHub/tnm-articles/articles/inngest-series/20261014/deployment run kb-mcp`

#### 3. Google Antigravity / Antigravity IDE
In Antigravity, MCP servers are declared in `mcp_config.json`. You can configure `living-kb` either globally across all workspaces or check it directly into your project's version control:
- **Project-Specific (Shared with Team)**: `.agents/mcp_config.json`
- **Global Configuration (Local Machine)**: `~/.gemini/config/mcp_config.json`

```json
{
  "mcpServers": {
    "living-kb": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/hedrergudene/Documents/GitHub/tnm-articles/articles/inngest-series/20261014/deployment",
        "run",
        "kb-mcp"
      ]
    }
  }
}
```

Antigravity auto-discovers the server on startup or session launch, discovers all eight tool schemas via stdio, and progressively injects them into the agent's context.

Once connected, your agent gains direct, native access to all eight tools across your terminal or IDE:

```
> Antigravity / Claude Code: Check the health of our knowledge base and list the indexed publications.
Tool Call: kb_health() -> {"status": "ok", "database": "ok", "inngest": "ok", "articles": 142, "passages": 810}
Tool Call: kb_list_publications() -> {"publications": [{"name": "The Neural Maze", "articles": 24}, ...]}

> Antigravity / Claude Code: Search for recent findings on test-time compute scaling.
Tool Call: kb_search({"query": "test-time compute scaling", "limit": 3}) -> {"results": [...]}
```

The agent queries storage in sub-second latency, grounds its code suggestions on your verified newsletter corpus, and triggers ingestion jobs asynchronously without ever locking its conversation thread.

---

# Summary & The Road to Week 4

In Lesson 3, we transitioned our pipeline from an isolated ingestion script into an adaptive, living knowledge network:

- We embraced **Good Web Citizenship**, slashing compute and network overhead by 99% using HTTP conditional requests (`ETag` / `If-None-Match`).
- We eliminated the update dilemma with **Temporal Supersession**, preserving complete citation histories via `valid_to` timestamps and PostgreSQL partial unique indexes.
- We turned operational runtime anomalies into an asset by capturing `inngest/function.failed` events into a dedicated **Failures Dataset**.
- We empowered AI developer agents by shipping an official **Model Context Protocol (MCP)** server, adhering strictly to the Asymmetry Principle across eight specialized tools.

### Next in Week 4: Structured Claims and Graph Fusion with Memgraph

In **Lesson 4**, we cross an extraordinary architectural threshold. We transition from retrieving flat text passages to extracting structured, verifiable **entity claims** with quote-span validation. 

We will deploy **Memgraph** alongside PostgreSQL, construct an attributed knowledge graph of authors, publications, and conceptual assertions, and upgrade our retrieval pipeline from two-way (sparse + dense) to **three-way fusion** by incorporating Cypher graph traversals into our reciprocal rank calculations.

Until then, explore the complete implementation in [`deployment/`](deployment/), connect your MCP server to Claude Code or Cursor, and keep engineering through the maze! 🚀
