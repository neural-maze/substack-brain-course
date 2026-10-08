# Week 2 — Three newsletters, flow control, and answers with citations

**Course framing:** *Point the week 1 pipeline at more than one newsletter
without getting banned, rate-limited or stuck in a queue, and give it a way
to answer questions that you can debug later.*

This guide assumes you finished week 1: the stack starts with `make start`,
and you've ingested The Neural Maze at least once. Everything here runs on
the same containers and the same database. `make start` applies this week's
only migration (the `snapshots` table) on its own.

## What you're building

By the end of this guide you will have three publications ingesting in
parallel, each with its own concurrency budget, all sharing one throttle that
protects your OpenAI quota. You'll be able to push a single urgent article
ahead of everything queued, re-run every backfill for free, search with
keyword and vector search fused into a single ranking, and ask questions that
come back with inline citations and a stored snapshot of everything the
system saw.

```
POST /publications ──▶ kb/publication.added ──▶ backfill-publication ──▶ kb/article.discovered (via=backfill) ─┐
POST /articles ────────────────────────────────────────────────────────▶ kb/article.discovered (via=manual)  ──┤
                                                                                                                ▼
                                          ingest-article  [concurrency per publication · global throttle · priority]

GET /search ──▶ full-text + vector ──▶ RRF ──▶ passages
POST /ask ────▶ full-text + vector ──▶ RRF ──▶ Claude ──▶ snapshots row ──▶ kb/answer.produced (fire-and-forget)
```

## First, an idea: what goes through Inngest, and what doesn't

Week 1 sent everything through Inngest because everything was ingestion.
This week adds the first endpoint where someone is waiting for the result,
and it deliberately does **not** go through Inngest.

The rule we follow: a workflow engine earns its place where there's
expensive work you'd hate to lose. Ingesting an article is slow, costs money
(embeddings) and nobody is watching, so if it dies at the `embed` step you
want to resume from the `embed` step. Answering a question is the opposite:
someone is waiting, nothing expensive is half-done, and retrying three
minutes later is useless because the user has gone. So `POST /ask` and
`GET /search` query Postgres directly. `POST /ask` sends one event,
`kb/answer.produced`, *after* it has saved its snapshot, so that the
evaluators we build in week 5 can do their slow work in the background.

## Step 1 — Add your Anthropic key

Open `.env` and set `ANTHROPIC_API_KEY`. It's only used by `POST /ask` to
write the answer (the model is `synthesis_model` in `kb/config.py`). Without
it, `/ask` still runs retrieval and returns the passages it found, with a
note telling you to set the key.

`.env.example` also has two new knobs, `CONCURRENCY_PER_PUBLICATION` and
`INGEST_RUNS_PER_MINUTE`. The defaults (3 and 50) are fine for normal use.
For the queue-jump demo in Step 4, set them to `1` and `10`.

## Step 2 — Backfill three newsletters at once

`publications.yaml` now lists Decoding AI and Ahead of AI next to The Neural
Maze, each with `archive_urls` so that both the custom domain and the
`*.substack.com` host are allowed.

```bash
curl -X POST localhost:8000/publications -H "Content-Type: application/json" \
  -d '{"feed_url": "https://theneuralmaze.com/feed"}'
curl -X POST localhost:8000/publications -H "Content-Type: application/json" \
  -d '{"feed_url": "https://www.decodingai.com/feed"}'
curl -X POST localhost:8000/publications -H "Content-Type: application/json" \
  -d '{"feed_url": "https://magazine.sebastianraschka.com/feed"}'
```

These now trigger `backfill-publication` (`kb/functions/backfill.py`)
instead of week 1's `add-publication`. Both listen to the same event, so
`kb/functions/__init__.py` registers only the new one. The backfill takes
every entry in the feed rather than the latest five, skips the articles that
are already stored, creates a `jobs` row, and sends the rest as
`kb/article.discovered` events with `via="backfill"`, in batches of
`backfill_batch_size`.

One thing that surprised us: a Substack RSS feed only carries a
publication's most recent posts (around twenty), so "the whole feed" is not
the whole archive. For this week that's plenty.

`GET /jobs/{event_id}` works as in week 1. It now counts only articles
written *after* the job started, so a backfill of a publication that already
had articles doesn't look finished before it begins.

## Step 3 — The three knobs on `ingest-article`

Open `kb/functions/ingest.py`. The steps are unchanged from week 1; what's
new is all in the decorator, which means Inngest enforces it in its queue
before any of our code runs.

**Concurrency, keyed by publication.** `key="event.data.publication_id"`
gives every publication its own limit of `concurrency_per_publication`
in-flight articles. Without the key there would be one shared limit, and
whichever backfill fanned out first would hog it. With the key, each website
sees at most N requests from us at once, and no backfill waits on another.

**Throttle, global.** `inngest.Throttle(limit=ingest_runs_per_minute,
period=1 minute)` has no key on purpose: your OpenAI quota is shared by
every publication. Throttling limits run *starts*, not the steps inside a
run, so it only protects a quota if each run makes a predictable number of
calls. `embed_texts` sends up to 96 chunks per request, which for a
newsletter post means one request per article, so "runs per minute" is
roughly "embedding requests per minute".

**Priority.** This is the one that's easy to get wrong. Inngest's priority
expression returns a **time shift in seconds**, between -600 and 600, not a
rank. `+600` means "schedule this run as if it had arrived ten minutes
earlier". We give manual runs `+600` and backfill runs `-600`, which puts a
manual article up to twenty minutes ahead of anything already queued. With a
smaller value like `100`, a manual article sent two minutes after a backfill
would still land behind it.

There's also something deliberately missing: an Inngest `idempotency` key.
It would stop the same article from being re-sent for 24 hours, which breaks
week 1's Step 9 (re-add the publication to retry the articles that failed).
Duplicates are already prevented by the backfill's "skip what's stored"
filter and by the upserts in `kb/db/queries.py`.

## Step 4 — The queue-jump demo (try this yourself)

With `CONCURRENCY_PER_PUBLICATION=1` in `.env` (restart the app after
changing it), trigger the three backfills from Step 2 and, straight away,
submit one Neural Maze post by hand:

```bash
curl -X POST localhost:8000/articles -H "Content-Type: application/json" \
  -d '{"url": "https://www.theneuralmaze.com/p/<a-real-post-slug>"}'
```

`POST /articles` checks the URL against the allowlist (a `400` if it's not
there), and sends a single `kb/article.discovered` event with
`via="manual"`. In the Inngest UI at `localhost:8288`, watch the
`ingest-article` runs: the manual one starts on the next free Neural Maze
slot, ahead of every Neural Maze backfill run still waiting. It doesn't
interrupt the run already in progress, and it doesn't take a slot from
another publication.

## Step 5 — Replays are free

```bash
uv run python scripts/replay_backfill.py --verify
uv run python scripts/replay_backfill.py --all
# wait for the runs to finish in the Inngest UI
uv run python scripts/replay_backfill.py --verify
```

`--verify` prints the number of publications, articles, passages and
embedding calls (rows in `costs` for the embedding model, written by week
1's `CostCaptureMiddleware`). `--all` re-sends `kb/publication.added` for
every publication. The second `--verify` should print exactly the same
numbers: the backfill filters out everything already stored, so no article
reaches the `embed` step again.

## Step 6 — Hybrid search

`GET /search` now defaults to `mode=hybrid`. Compare the three modes on a
query with an exact token in it:

```bash
curl "localhost:8000/search?q=create_function&mode=sparse"
curl "localhost:8000/search?q=create_function&mode=dense"
curl "localhost:8000/search?q=create_function"
```

`kb/retrieval/hybrid.py` asks each engine for 15 candidates and fuses them
with Reciprocal Rank Fusion: every passage gets `1 / (k + rank)` from each
engine that found it, with `k = 60`. It never adds the engines' raw scores,
because `ts_rank` and cosine similarity live on different scales. A passage
found by both engines collects points twice, so agreement beats being one
engine's favourite. Each result carries `sparse_rank`, `dense_rank` and
`source` (`bm25`, `vector` or `both`), and ties are broken by `passage_id`
so the same query always returns the same order.

Something we hit while building it: the first version ran the two searches
with `asyncio.gather` on the same database session. It seemed to work,
because the vector search waits for OpenAI before touching the database, so
the two queries rarely overlapped. Rarely isn't never, and SQLAlchemy's
`AsyncSession` refuses concurrent queries. The current version overlaps the
OpenAI call with the full-text query and runs the vector query afterwards.

## Step 7 — Ask a question

```bash
curl -X POST localhost:8000/ask -H "Content-Type: application/json" \
  -d '{"question": "How do durable execution engines handle failure recovery compared to traditional task queues?"}'
```

`kb/functions/ask.py` sends the top `retrieval_limit` fused passages to
Claude, numbered `[1]` to `[5]`, with a system prompt that asks for an
answer grounded only in those passages and cited inline. Only the markers
that actually appear in the answer become `citations`. `as_of` is the
publication date of the newest cited source, so you know how fresh the
answer is.

Before returning, it writes a row to `snapshots` with the question, the
answer, the citations, the latency, the retriever variant
(`hybrid_rrf_k60`) and **every** candidate from both engines, cited or not.
Then it sends `kb/answer.produced` without waiting for it. The snapshot is
committed first, so a lost event never means lost data.

Look at what got stored:

```bash
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U substack_brain -d substack_brain -c \
  "SELECT snapshot_id, latency_ms, jsonb_array_length(retrieved) AS candidates,
          jsonb_array_length(citations) AS cited FROM snapshots ORDER BY created_at DESC LIMIT 5;"
```

There are always more candidates than citations, and that gap is the point.
When an answer is wrong, the snapshot tells you whether the right passage
was never retrieved (a retrieval problem) or retrieved and ignored (a
generation problem).

## Verification checklist

- `make check` passes (the new tests are in `tests/test_week2.py`, and like
  week 1's they need no Docker or API keys).
- Three backfills run in parallel, each limited to its own concurrency.
- A manual article submitted during a backfill starts ahead of the queued
  backfill runs for the same publication.
- `replay_backfill.py --verify` prints the same numbers before and after
  `--all`.
- `GET /search` returns fused results with `source` set to `bm25`, `vector`
  or `both`.
- `POST /ask` returns an answer with citations and a `snapshot_id`, and the
  snapshot is in the `snapshots` table.
