# Substack Brain — Week 1: your first durable pipeline

A small knowledge base built from a technical newsletter. It reads articles
from an RSS feed, splits them into passages, embeds them, stores everything in
Postgres, and lets you search them two different ways. Orchestration is
[Inngest](https://www.inngest.com/), and everything runs locally with Docker.

The point of week 1 is **durable execution**: each step of the pipeline is
recorded when it succeeds, so if the process dies halfway through, it resumes
from the failed step instead of starting over (and instead of paying for the
same API call twice).

```
POST /publications ─▶ add-publication ─▶ ingest-article (×5) ─▶ Postgres
                      (read the feed,     (fetch → parse → chunk
                       pick latest 5)      → embed → save)
```

## Getting started

You need Docker and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/neural-maze/substack-brain-course.git
cd substack-brain-course
cp .env.example .env        # fill in OPENAI_API_KEY (ANTHROPIC_API_KEY can stay empty)
make start                  # docker compose up, wait healthy, migrate, run the app
```

`make start` stays in the foreground, so use a second terminal for the rest.

Trigger your first ingestion with plain HTTP:

```bash
curl -X POST localhost:8000/publications \
  -H "Content-Type: application/json" \
  -d '{"feed_url": "https://theneuralmaze.substack.com/feed"}'
# → {"event_id": "...", "trace_url": "http://localhost:8288/event/...", "status": "queued"}

curl localhost:8000/jobs/<event_id>
# → {"status": "completed", "articles_done": 5, "articles_total": 5, ...}

curl "localhost:8000/search?q=agents&mode=sparse"   # BM25 (Postgres full-text)
curl "localhost:8000/search?q=agents&mode=dense"    # pgvector cosine similarity
```

For the full walkthrough, including the kill/resume demo, read
**[`docs/week-1.md`](docs/week-1.md)**.

## Running the tests

```bash
make test
```

The tests are in [`tests/test_basics.py`](tests/test_basics.py). They need no
Docker, database or API key, and run in about a second. They cover the small
functions the pipeline is built from: URL cleanup, the allowlist, chunking and
RSS parsing.

## Useful commands

```bash
make check     # lint (ruff + mypy) and tests
make stop      # docker compose down (your data is kept)
make db-clear  # destructive: empties every table, keeps the schema
make reset     # destructive: docker compose down -v, drops all data
make nuke      # destructive: reset, plus kills a stray app process on the port
```

## Tools for looking inside

| Tool | URL | For |
|---|---|---|
| **Adminer** | `localhost:8081` | Browse Postgres and watch rows land in `articles` and `passages`. |
| **Inngest dev server** | `localhost:8288` | Every function, run and step, with its timeline and retries. |
| **Swagger UI** | `localhost:8000/docs` | Try every endpoint from the browser. |

## Repository layout

- `kb/` — the pipeline: `functions/` (Inngest), `sources/` (fetch, parse, chunk, embed), `db/`, `retrieval/`, `schemas/`
- `publications.yaml` — the allowlist of feeds the pipeline may ingest
- `alembic/` — database migrations
- `infra/docker-compose.yml` — Postgres (pgvector), Adminer, Inngest dev server
- `docs/week-1.md` — the step-by-step guide
- `tests/` — the simple tests
