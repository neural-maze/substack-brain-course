# Week 3 — Conditional freshness, temporal supersession, and an MCP server

**Course framing:** *Turn your pipeline into a polite, living subscriber of the web, handle article edits without destroying citation history, capture runtime failures for evals, and give agents an official MCP interface.*

This guide is written so you can follow it top to bottom, whether you finished Week 2 or are **starting fresh from a clean state** (`make nuke` / fresh clone). Every command below can be run against this repo state.

The main [README.md](../README.md) has the high-level course overview. This file is the full walkthrough.

## What you're building

In production, technical newsletters aren't static. Authors edit code snippets, fix typos, and publish addendums. Naive pipelines either hammer origin feeds with redundant XML parsing, overwrite past database rows (breaking earlier citation snapshots), or duplicate passages (poisoning search with contradictory versions).

By the end of this guide, you will have:

- **Polite freshness polling (`poll-feeds`)**: Uses HTTP conditional headers (`ETag`, `If-None-Match`, `Last-Modified`) on a 5-minute cron. When nothing has changed, origins return `304 Not Modified` with 0 body bytes, and the run finishes in under 15ms.
- **Non-destructive temporal supersession**: Edits are detected via SHA-256 `content_hash`. Old passages get `valid_to = now()`, new ones get `valid_to = NULL`, and a partial unique index keeps chunk indexes clean without deleting history.
- **An operational failure collector (`handle-function-failed`)**: Listens for Inngest's native `inngest/function.failed` events and records them in a `failures` table, seeding our week 5 eval suite with real production anomalies.
- **An MCP server (`kb-mcp`)**: Exposes 8 typed tools to Claude Code and Cursor over stdio, split into fast reads (<1s directly to Postgres) and slow writes (<50ms event dispatch to Inngest).

```
kb/feed.poll / cron (*/5) ──▶ poll-feeds ──▶ 304 Not Modified (<15ms, 0 parsing)
                                         ──▶ 200 OK ──▶ kb/article.discovered
                                                              │
                                                              ▼
                                                        ingest-article
                                                [unchanged]   [content edit]
                                                     │              │
                                                  short-circuit  supersede old (valid_to=now)
                                                                 insert new (valid_to=NULL)
                                                                 emit kb/article.updated

inngest/function.failed ──▶ handle-function-failed ──▶ failures table

Claude Code / Cursor ──▶ kb-mcp ──▶ Fast reads (<1s, Postgres): kb_health, kb_search, kb_ask, kb_list_publications, kb_job_status
                                ──▶ Slow writes (<50ms, Inngest): kb_add_pub, kb_backfill, kb_sync
```

## First, an idea: the asymmetry principle of agent tools

When an AI agent in Claude Code or Cursor calls a tool, two failure modes are common:

1. **Slow writes timeout**: Scraping or embedding 20 articles inside a synchronous tool handler blocks stdio and trips client timeouts (typically 30–60s).
2. **Fast reads add latency**: Putting search behind a background queue adds seconds of orchestration overhead to an interactive conversation.

The **Asymmetry Principle** resolves this by enforcing a hard boundary:

- **Fast Reads (<1s)**: `kb_health`, `kb_search`, `kb_ask`, `kb_list_publications`, and `kb_job_status` query Postgres, run hybrid retrieval, or query Claude directly. They never emit Inngest events and never wait on a queue.
- **Slow Writes (<50ms)**: `kb_add_publication`, `kb_backfill`, and `kb_sync` validate inputs, send an Inngest event, and return immediately with `{event_id, trace_url, status: "queued"}`. The agent stays responsive and can poll `kb_job_status(event_id)` or follow the trace URL.

The MCP server introduces zero new orchestration logic: it is purely an ergonomic wrapper around queries and events that already exist.

---

## Step 0 — Fresh start setup

If you are continuing with an existing database from week 2, you can skip straight to **Step 1**.

If you are starting fresh, configure your environment:

1. **Set up `.env`**:
   ```bash
   cp .env.example .env
   ```
   Open `.env` and fill in:
   - `OPENAI_API_KEY`: Required for embeddings (`text-embedding-3-small`).
   - `ANTHROPIC_API_KEY`: Optional; used by `POST /ask` and `kb_ask` synthesis. Without it, retrieval still works.
   - `PRIORITY_BACKFILL_S=0`: *(Optional pro-tip)* Week 2 defaults backfill priority to `-600` (a 10-minute queue delay to let manual submissions jump ahead). If you are testing backfills locally and want them to process immediately, set `PRIORITY_BACKFILL_S=0`.

2. **Start the stack**:
   ```bash
   make start
   ```
   This launches Postgres (pgvector), Adminer, and the Inngest Dev Server via Docker Compose, runs Alembic migrations up to head, and starts FastAPI at `http://localhost:8000`. Leave this terminal open.

---

## Step 1 — Migrations: failures and partial indexes

`make start` automatically applies `alembic/versions/e4a7b9c1d2e3_week_3_failures_table.py`. This migration does two things:

1. Creates the `failures` table (`id`, `function_id`, `run_id`, `error`, `event_data`, `created_at`).
2. Drops week 1's unconditional unique index on `passages` and replaces it with a **partial unique index**:

```sql
CREATE UNIQUE INDEX uq_passages_article_chunk 
ON passages (article_id, chunk_index) 
WHERE valid_to IS NULL;
```

A standard unique constraint would block inserting chunk 0 of an updated article while chunk 0 of the superseded revision still exists. The partial index enforces uniqueness only across active passages (`valid_to IS NULL`), allowing historical revisions to coexist in storage.

Verify the migration in Postgres:

```bash
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U substack_brain -d substack_brain -c \
  "\d passages"
```
Look for `uq_passages_article_chunk UNIQUE, btree (article_id, chunk_index) WHERE valid_to IS NULL`.

---

## Step 2 — Conditional polling (the 304 fast path)

Open `kb/sources/rss.py`. When checking feeds for updates, `fetch_feed_conditional` passes the stored validators from the `cursors` table:

```python
headers = {"User-Agent": settings.user_agent}
if etag:
    headers["If-None-Match"] = etag
if last_modified:
    headers["If-Modified-Since"] = last_modified
```

`poll-feeds` (`kb/functions/freshness.py`) runs every 5 minutes on a cron (`*/5 * * * *`) and also listens for the `kb/feed.poll` event.

### The Two-Phase Lifecycle: Cold Start vs. Fast Path

1. **Run 1: Cold Start (The First Poll)**
   On a clean database, `cursors` has no record of `ETag` or `Last-Modified`. Triggering a poll for the first time fetches the feeds without validation headers:
   ```bash
   curl -X POST http://localhost:8288/e/local \
     -H "Content-Type: application/json" \
     -d '{"name": "kb/feed.poll", "data": {}}'
   ```
   - Origins return `HTTP 200 OK`.
   - The response headers (`ETag`, `Last-Modified`) are saved to the `cursors` table.
   - Any new articles found fan out to `ingest-article`.

2. **Run 2: The Fast Path (`304 Not Modified`)**
   Trigger the poll event a second time:
   ```bash
   curl -X POST http://localhost:8288/e/local \
     -H "Content-Type: application/json" \
     -d '{"name": "kb/feed.poll", "data": {}}'
   ```
   Now check the Inngest Dev Server (`http://localhost:8288`):
   - The run completes across all allowlisted publications in milliseconds.
   - The origin returns `HTTP 304`.
   - 0 bytes of feed XML are transferred or parsed.
   - `cursors.not_modified_count` increments in Postgres.

Check the cursors table:

```bash
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U substack_brain -d substack_brain -c \
  "SELECT p.slug, c.etag, c.not_modified_count, c.poll_count FROM cursors c JOIN publications p ON c.publication_id = p.id;"
```

Only when an author posts new content does the feed return `200 OK`, updating the cursor and emitting `kb/article.discovered` for the new items.

---

## Step 3 — Article edits and temporal supersession

When an article event arrives at `ingest-article` (`kb/functions/ingest.py`), the `check-content-change` step compares SHA-256 `content_hash`:

- **Identical hash**: Short-circuits with `status: "unchanged"`, burning zero OpenAI embedding credits.
- **Changed hash (`is_update=True`)**: Enters temporal supersession.

```python
if is_update:
    await supersede_passages(session, article_id=article_id)
    passage_count = await insert_passages_fresh(
        session, article_id=article_id, passages=passages_data
    )
```

`supersede_passages` seals active rows with `valid_to = func.now()`. Fresh chunks are inserted with `valid_to = NULL`, and `ArticleUpdated` is emitted.

### Try it yourself: Simulate an article update

You don't have to wait for an author to edit a post to see supersession work. Try this hands-on test:

1. **Ingest an article manually**:
   ```bash
   curl -X POST http://localhost:8000/articles -H "Content-Type: application/json" \
     -d '{"url": "https://www.theneuralmaze.com/p/the-complete-guide-to-event-driven"}'
   ```
   *(Manual ingestion tags `via="manual"`, giving it `+600` priority so it runs immediately).*

2. **Simulate an upstream edit**:
   In Postgres, tamper with the stored `content_hash` so the pipeline detects a change on its next run:
   ```bash
   docker compose -f infra/docker-compose.yml exec postgres \
     psql -U substack_brain -d substack_brain -c \
     "UPDATE articles SET content_hash = 'stale_hash_for_testing' WHERE url = 'https://www.theneuralmaze.com/p/the-complete-guide-to-event-driven';"
   ```

3. **Re-submit the article**:
   ```bash
   curl -X POST http://localhost:8000/articles -H "Content-Type: application/json" \
     -d '{"url": "https://www.theneuralmaze.com/p/the-complete-guide-to-event-driven"}'
   ```

4. **Inspect the superseded passages**:
   ```bash
   docker compose -f infra/docker-compose.yml exec postgres \
     psql -U substack_brain -d substack_brain -c \
     "SELECT p.chunk_index, p.valid_from, p.valid_to, LEFT(p.text, 50) AS snippet FROM passages p JOIN articles a ON p.article_id = a.id WHERE a.url = 'https://www.theneuralmaze.com/p/the-complete-guide-to-event-driven' ORDER BY p.chunk_index, p.valid_from;"
   ```

Notice what happened:
- Old passages now have `valid_to = <timestamp>`.
- Fresh passages have `valid_to = NULL`.
- Historical passages were never deleted.
- Search queries (`GET /search` and `POST /ask`) filter `WHERE valid_to IS NULL`, so search returns only active revisions with zero duplicate chunks.

---

## Step 4 — Turn failures into an eval dataset

When any Inngest function fails its retries or hits a non-retriable exception, Inngest synthesizes an `inngest/function.failed` event.

`kb/functions/ops.py` registers `handle-function-failed` to record these directly into the `failures` table:

```python
@client.create_function(
    fn_id="handle-function-failed",
    name="Record Function Failures",
    trigger=inngest.TriggerEvent(event="inngest/function.failed"),
)
async def handle_function_failed(ctx: inngest.Context) -> dict[str, object]:
    ...
```

Instead of synthetic test data, our week 5 benchmark suite will replay genuine production failures captured here.

### Try it yourself: Capture an Inngest failure

Simulate an operational failure event:

```bash
curl -X POST http://localhost:8288/e/local -H "Content-Type: application/json" \
  -d '{
    "name": "inngest/function.failed",
    "data": {
      "error": {"message": "Simulated upstream rate limit error for week 5 evals"},
      "function_id": "ingest-article"
    }
  }'
```

Check the `failures` table in Postgres:

```bash
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U substack_brain -d substack_brain -c \
  "SELECT function_id, error, created_at FROM failures;"
```

The failure is persisted with its payload and ready to be replayed during evals.

---

## Step 5 — The 8 MCP tools

The MCP server is declared in `kb/mcp/server.py` and implements 8 tools in `kb/mcp/tools.py`:

| Tool | Type | What it does |
| :--- | :--- | :--- |
| `kb_health()` | Read | Checks Postgres and Inngest reachability, reports counts. |
| `kb_search(query, limit)` | Read | Hybrid BM25 + pgvector RRF search over active passages (`valid_to IS NULL`). |
| `kb_ask(question)` | Read | Hybrid search + Claude synthesis with inline citations and snapshot storage. |
| `kb_list_publications()` | Read | Lists indexed publications, feed URLs, and article counts. |
| `kb_job_status(event_id)` | Read | Combines local `jobs` table with live Inngest `/v1/events/{id}/runs` progress. |
| `kb_add_publication(feed_url)` | Write | Validates allowlist, dispatches `kb/publication.added`, returns handle in <50ms. |
| `kb_backfill(publication)` | Write | Queues backfill runs for one or all allowlisted publications. |
| `kb_sync(publication)` | Write | Dispatches `kb/feed.poll` to trigger immediate conditional check. |

---

## Step 6 — Connect Claude Code and Cursor

The server runs via stdio (`kb-mcp` console script from `pyproject.toml`).

### 1. Test directly from the terminal first

Before configuring IDE plugins, verify that the server runs and all tools respond:

**Option A: Run the integration tests**
```bash
uv run python -m pytest tests/integration/test_mcp_tools.py tests/integration/test_mcp_server.py
```
These tests talk to the real Postgres and Inngest Dev Server, so keep `make start` running in another terminal. If the stack is down they are reported as `skipped`, not failed, so check that the summary says `passed`.

**Option B: Test interactively with MCP Inspector**
```bash
npx @modelcontextprotocol/inspector uv run kb-mcp
```
This opens a web UI where you can inspect tools and invoke `kb_health`, `kb_list_publications`, and `kb_search` in real time.

---

### 2. Configure Claude Code (`~/.claude.json`)

```json
{
  "mcpServers": {
    "substack-brain": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/substack-brain-course", "run", "kb-mcp"]
    }
  }
}
```

### 3. Configure Cursor

Open **Settings** ➔ **Features** ➔ **MCP** ➔ **Add New MCP Server**:
- **Name**: `substack-brain`
- **Type**: `command`
- **Command**: `uv --directory /absolute/path/to/substack-brain-course run kb-mcp`

### Try it in chat

Ask your agent:
- *"Check the health of our knowledge base and list the publications we have indexed."*
- *"Search our knowledge base for reciprocal rank fusion."*
- *"Force an immediate freshness check for all publications."*

Fast reads return in under a second; writes queue an Inngest event and return a trace URL immediately without freezing the chat.

---

## Step 7 — Run the tests

Unit tests in `tests/test_week3.py` verify triggers, schemas, and MCP tool registration without Docker or API keys:

```bash
uv run python -m pytest tests/test_week3.py
```

Run the complete test and lint suite:

```bash
make check
```

---

## Verification checklist

- `make check` passes (`ruff check .`, `mypy`, `pytest`).
- Database migrations applied: `failures` table exists and `passages` has `uq_passages_article_chunk ... WHERE valid_to IS NULL`.
- Conditional polling: triggering `kb/feed.poll` on subsequent runs returns 304 and increments `not_modified_count` in `cursors`.
- Supersession: updating an article sets `valid_to` on existing chunks, inserts new chunks with `valid_to = NULL`, and emits `kb/article.updated`.
- Search filtering: `GET /search` and `POST /ask` retrieve only active passages (`valid_to IS NULL`).
- Failure recording: `inngest/function.failed` creates rows in the `failures` table.
- MCP Server: `uv run kb-mcp` exposes all 8 tools over stdio.
- Agent integration: Claude Code or Cursor connects to `substack-brain` and executes `kb_health`, `kb_search`, and `kb_sync`.

