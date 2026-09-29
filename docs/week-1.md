# Week 1 — Foundations: your first durable pipeline, and a first look at retrieval

**Course framing:** *Your first durable pipeline, triggered the plain way
Inngest intends — and a first, light look at how you'll eventually search it.*

This guide is written so you can follow it top to bottom on a fresh machine
and end up with a real, working system — not just read about one. Every
command below has actually been run against this exact repo state. Where
something surprised us while building it, we kept it in, because that's
what makes this a lesson instead of a demo.

The main [`README.md`](../README.md) has the short version of setup. This
file is the full walkthrough.

## What you're building

By the end of this guide you will have: a Postgres database holding real
articles from a real newsletter, an orchestration layer (Inngest) that
survives being killed mid-task, triggered entirely over plain HTTP, and a
first taste of two different ways to search what you ingested.

There's no MCP server or agent layer — deliberately. You talk to the system
the way Inngest's own docs describe: send an event, get a handle back, poll
for status.

```
kb/publication.added ──▶ add-publication ──▶ kb/article.discovered (×5) ──▶ ingest-article ──▶ kb/article.ingested
   (POST /publications)  (fetch feed,                                       (fetch → parse →
                           upsert pub,                                        chunk → embed →
                           fan out latest 5)                                  persist → emit)
```

## First, an idea: what is Inngest, and why not just a queue or a cron job?

Skip this section if you already know what durable execution means. If not,
here's the two-minute version.

A normal background job — a Celery task, a plain cron script — runs top to
bottom in one process. If that process dies halfway through (the machine
reboots, it OOMs, someone hits Ctrl+C), the whole job is gone. You either
re-run it from scratch (wasting whatever work it already did — and if that
work included calling a paid API, you're paying for it twice) or you write
your own bespoke checkpointing logic to resume partway through. Most teams
do neither, and just accept that background jobs occasionally silently fail.

**Inngest's idea**: you write your function as a sequence of named `step`s.
Each step's *result* — not just "it ran," the actual return value — is
durably recorded the moment it succeeds. If your process dies after step 3
of 7 completes, restarting the same run does not re-execute steps 1–3 at
all — it replays your function from the top, but steps 1–3 return their
already-recorded results instantly, without re-running your code, and
execution proceeds fresh from step 4. This is not a queue with retries
bolted on; the *unit of retry* is the individual step, not the whole job.

That single idea is what this whole week demonstrates. `kb/functions/ingest.py`
has 7 steps for exactly this reason — so a failure in step 5 (`embed`, which
calls a paid API) never repeats steps 1–4 (fetching and parsing the
article).

The second idea worth knowing now: Inngest is **event-driven**. Functions
don't call each other directly. `add-publication` doesn't call
`ingest-article` — it emits an event (`kb/article.discovered`), and Inngest
routes it to whichever function is listening. Neither function knows or
cares how many listeners exist. This is what lets you add a completely
new consumer of that same event without touching a single line of the
producer. `kb/main.py`'s `POST /publications` doesn't know about
`ingest-article` either — it only sends `kb/publication.added` and returns.

## Step 1 — Clone and configure

```bash
git clone https://github.com/neural-maze/substack-brain-course.git
cd substack-brain-course
cp .env.example .env
```

Open `.env` and fill in:
- `ANTHROPIC_API_KEY` — not used in this week, you can leave it empty.
- `OPENAI_API_KEY` — used by the `embed` step and by the dense
  search endpoint's query embedding. Without a funded account, ingestion will
  fail at exactly that step (see the real story in the "What actually
  happened" section below — it happened to us).

Everything else in `.env.example` has a sane local default; you don't need
to touch it.

## Step 2 — `make start`, and what's actually running

```bash
make start
```

This one command does four things, in order: brings up Docker Compose,
waits for the services with healthchecks to report healthy, runs the
database migrations, and starts the FastAPI app. It runs in the
**foreground** — leave this terminal open and do everything else in a
second one.

Here's what's actually in `infra/docker-compose.yml`, and why each thing is
there — not just "these are the containers," but what job each one does in
the architecture:

| Service | Port | Why it's here |
|---|---|---|
| **postgres** (pgvector) | 5433 | Owns *all* operational state: article text, chunk embeddings, BM25 search vectors, job tracking, cost logs. One database for text + vectors + full-text search means no separate vector-DB to keep in sync. |
| **adminer** | 8081 | A tiny (≈20MB) web UI for browsing Postgres directly. Not part of the product — purely so you can *see* what ingestion actually wrote, without memorizing `psql` syntax. |
| **inngest-dev** | 8288 (UI/API), 8289 (connect) | The orchestration engine itself. Runs every `kb/functions/*.py` function, retries failed steps, and gives you a UI to inspect every run. |

The FastAPI app (`kb/main.py`) is **not** a container — `make start` runs it
directly on your machine (via `uv run uvicorn`) so file changes reload
instantly while you're building. Inngest's dev server reaches it at
`http://host.docker.internal:8000/api/inngest`.

Once it's up, confirm everything is actually talking to everything else:

```bash
curl localhost:8000/health
```
```json
{"status": "ok", "database": "ok", "inngest": "ok", "articles": 0, "passages": 0}
```

Zero articles is correct — you haven't ingested anything yet.

FastAPI generates interactive API docs for free from the endpoints in
`kb/main.py` — open **http://localhost:8000/docs** (Swagger UI) or
**http://localhost:8000/redoc**. Every `curl` command in this guide has an
exact equivalent there: expand an endpoint, click **Try it out**, fill in
the fields, **Execute**. If you'd rather click than type a request body by
hand, use this instead of `curl` for the rest of the guide — same requests,
same responses, and you can see the full request/response schema for each
endpoint (including `/search`'s `mode` enum) without reading the source.

## Step 3 — Look inside Postgres (Adminer)

Open **http://localhost:8081**. Two things trip people up on this login
screen, so watch for them:

| Field | Value | Watch out for |
|---|---|---|
| System | **PostgreSQL** | Defaults to "MySQL / MariaDB" — you must change this dropdown yourself. |
| Server | `postgres` | Already pre-filled correctly (`ADMINER_DEFAULT_SERVER` in `docker-compose.yml`) — it's the Docker Compose service name, not `localhost`, since Adminer is itself a container on the same Docker network. Adminer's own default here is `db`, which doesn't exist in this project — that's the #1 way to get stuck on this screen. |
| Username | `POSTGRES_USER` from your `.env` (`substack_brain` by default) | |
| Password | `POSTGRES_PASSWORD` from your `.env` (`substack_brain_pw` by default) | |
| Database | `POSTGRES_DB` from your `.env` (`substack_brain` by default) | |

You'll see six empty tables: `publications`, `articles`, `passages`,
`cursors`, `jobs`, `costs`. Keep this tab open — you'll watch rows appear in
real time once you trigger ingestion in Step 5. This is genuinely one of
the better "aha" moments of week 1: watching a passage with a real
1536-number embedding vector materialize in a table you can click into.

## Step 4 — Meet the Inngest dev server

Open **http://localhost:8288**. Three tabs matter:

- **Functions** — lists every function this app registered (`add-publication`,
  `ingest-article`). Click one to see its configuration: triggers,
  concurrency limits, retry policy.
- **Runs** — every execution, past and present, with a status
  (`Running`/`Completed`/`Failed`) and a full step-by-step timeline once you
  trigger something.
- **Events** — every event this app has ever sent or received, raw JSON
  payload included.

Nothing has run yet, so these will be empty — that's expected. Come back
here after Step 5.

## Step 5 — Trigger your first real ingestion

```bash
curl -X POST localhost:8000/publications \
  -H "Content-Type: application/json" \
  -d '{"feed_url": "https://theneuralmaze.substack.com/feed"}'
```
```json
{"event_id": "01...", "trace_url": "http://localhost:8288/event/01...", "status": "queued"}
```

This returns immediately — it does **not** wait for ingestion to finish.
Fast reads (`GET /health`, `GET /search`) skip Inngest entirely and hit
Postgres directly; slow work (`POST /publications`) always returns a handle
instead of blocking. Open `trace_url`, or just switch to the Inngest dev
server tab from Step 4: you'll see `add-publication` run first (fetching the
feed, picking the 5 most recent posts), then 5 separate `ingest-article`
runs fan out, each with its own 7-step timeline.

Poll for completion:
```bash
curl localhost:8000/jobs/<event_id from above>
```
```json
{"status": "completed", "articles_done": 5, "articles_total": 5, "run_status": "Completed", "failures": [], "trace_url": "..."}
```

Flip back to your Adminer tab and actually look at what landed, not just
that something did:

1. Click **`publications`** in the left sidebar, then **Select data**. One
   row: the slug, name, feed URL and homepage the loader derived from the
   feed you posted.
2. Click **`articles`** → **Select data**. Five rows. Click into one —
   `canonical_id` is the deduped, normalized form of the URL (Step 1's `?
   ` params and trailing slashes stripped); `content_hash` is the sha256 of
   the cleaned text.
3. Click **`passages`** → **Select data**. More rows than articles — each
   article split into several chunks (Step 6 explains why). Click a row's
   `embedding` cell: Adminer renders the actual 1536-number vector inline.
   That's the real output of a real OpenAI API call, not a placeholder.
4. Use the **SQL command** tab (top nav) for anything a plain table browse
   can't show easily, for example:
   ```sql
   SELECT a.title, a.author, count(p.id) AS passage_count
   FROM articles a JOIN passages p ON p.article_id = a.id
   GROUP BY a.id, a.title, a.author
   ORDER BY a.published_at DESC;
   ```
   ```sql
   -- Confirm every passage actually got an embedding (should equal the
   -- passage_count above for every row — a NULL here means the embed step
   -- never ran for that passage, worth knowing before Step 8's kill demo).
   SELECT count(*) FILTER (WHERE embedding IS NOT NULL) AS embedded,
          count(*) AS total
   FROM passages;
   ```

## Step 6 — How chunking actually works

Before embedding, each article's clean text is split into overlapping
chunks (`kb/sources/chunking.py`). Two config values control this
(`kb/config.py`, overridable via `CHUNK_SIZE`/`CHUNK_OVERLAP` in your
`.env`): `chunk_size=1000` (max characters per chunk) and
`chunk_overlap=150` (characters repeated between consecutive chunks).

Why overlap at all? If a sentence or an idea straddles exactly where a
chunk boundary falls, a non-overlapping split would leave *both* halves
of that idea incomplete on their own — bad for retrieval, since a search
might match only the half that doesn't have enough context to be useful.
Repeating the tail of one chunk at the head of the next means an idea near
a boundary is fully present in at least one chunk.

A toy illustration (not real ingested content) — splitting on whitespace,
never mid-word, with a small overlap:

```
Full text: "the quick brown fox jumps over the lazy dog while the cat watches"

Chunk 0: "the quick brown fox jumps over the lazy dog"
Chunk 1:                          "over the lazy dog while the cat watches"
                                    ^^^^^^^^^^^^^^^^ repeated from chunk 0
```

The real algorithm works in characters, not words, and the real chunk size
is ~1000 characters (this example uses tiny numbers only to make the
overlap visible). Every chunk becomes one row in `passages`, and gets its
own embedding vector independently.

## Step 7 — A first, light look at retrieval

You now have real passages with real embeddings. `GET /search` gives you
two independent, direct ways to query them — **no fusion, no LLM synthesis**.
Prove both work, and notice that they don't agree.

```bash
curl "localhost:8000/search?q=agents&mode=sparse"
```
This is **BM25** — Postgres's `tsvector` full-text search, ranking passages
by keyword relevance (`kb/retrieval/sparse.py` → `kb/db/queries.py`'s
`search_passages_bm25`). No LLM, no Inngest run, sub-second.

```bash
curl "localhost:8000/search?q=agents&mode=dense"
```
This is **dense retrieval** — your query is embedded with the same model
used for ingestion, then compared against every passage's embedding by
cosine similarity via pgvector (`kb/retrieval/dense.py` →
`search_passages_dense`). Also no Inngest run; the only extra cost is one
embedding API call for the query itself.

Try the same query against both modes and compare the top result and its
`rank` value. **They will very likely disagree on ordering, sometimes even
on which passages show up at all.** BM25 rewards exact keyword overlap;
dense retrieval rewards semantic similarity even when the wording differs.
Neither is "wrong" — they're finding different kinds of relevance. Neither
one alone is the whole answer.

## Step 8 — The kill/resume demo (try this yourself)

This is the signature demonstration of the whole course, and it's worth
doing with your own hands once:

1. Trigger an ingest for an article you haven't already ingested (any of
   the other posts in the feed works — check `articles` in Adminer for
   which canonical ids you already have).
2. Watch the terminal running `make start`. The moment you see log lines
   for the `embed` step, hit **Ctrl+C**. (Ctrl+C correctly signals the whole
   process group; killing by a guessed PID from another terminal is less
   reliable with `--reload` mode's separate reloader/worker processes.)
3. Run `make start` again.
4. Open the run in the Inngest UI (Step 4's tab).

**What you should see**: `check-allowlist`, `fetch-html`, `parse` and
`chunk` show as already completed in the timeline and are *not* re-executed
— only `embed` runs again, and the run completes normally from there.

We hit this for real, unscripted, while building week 1 — the OpenAI
account we were testing with ran out of credits mid-ingestion. The trace
showed exactly this: four steps completed and stayed completed, `embed`
retried automatically 3 times (per the `retries=3` config) and then failed,
and after adding credits, re-sending the same event resumed cleanly with
nothing upstream of `embed` re-running. A real provider outage demonstrated
the mechanism for free — you don't have to take our word for the guarantee.

## Step 9 — Confirm idempotency

```bash
# re-send the same POST /publications call with the same feed_url
curl -X POST localhost:8000/publications \
  -H "Content-Type: application/json" \
  -d '{"feed_url": "https://theneuralmaze.substack.com/feed"}'
```
Check Adminer's `articles`/`passages` row counts before and after — they
should be **identical**. Two things cooperate to guarantee this: `articles`
upserts on `canonical_id` and `passages` upserts on `(article_id,
chunk_index)` (so even a full re-run is a no-op), *and* `ingest-article` has
`idempotency="event.data.canonical_id"` configured at the Inngest level,
which prevents a duplicate run from even being created within a 24-hour
window. Worth knowing: that second mechanism blocks a *retry* too, not just
a duplicate — if a run permanently fails, re-sending its triggering event
won't create a new attempt until that window passes.

**If you're iterating locally** (re-triggering `POST /publications` for the
same feed over and over while poking at the system) and at some point new
articles just... stop appearing, with no error anywhere: this is that same
24-hour window, working exactly as designed, not something broken. The
feed's latest 5 articles have the same `canonical_id`s every time, so
re-triggering them repeatedly hits the rate limit almost immediately.
Since the local Inngest Dev Server keeps this state only in memory (no
volume in `docker-compose.yml`), `docker compose -f infra/docker-compose.yml
restart inngest-dev` resets it — fine for local dev, not something you'd do
against Inngest Cloud in production.

## Verification checklist

Before you consider week 1 "done" on your machine, confirm all of these —
this is the gate for the week:

- [ ] `make check` passes (ruff, mypy, pytest). The tests are in `tests/test_basics.py` and need no Docker.
- [ ] `GET /health` reports all subsystems `"ok"`.
- [ ] `POST /publications` → `GET /jobs/{event_id}` reaches `"completed"` with 5 articles.
- [ ] `GET /search?mode=sparse` and `?mode=dense` both return real passages with real titles/authors/URLs.
- [ ] Re-sending the same publication produces identical article/passage counts.
- [ ] You've done the kill/resume demo yourself and watched only `embed` re-run.

Once all of these hold, run `make stop` — your data stays in a Docker volume,
so `make start` picks up where you left off. Use `make reset` or `make nuke`
only when you want a genuinely empty corpus, or something's behaving
strangely and you want to rule out stale state.
