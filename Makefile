.PHONY: start stop logs reset nuke db-clear check lint test

-include .env
export

COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env
REPO_DIR := $(shell pwd)
LAB_PORT ?= 8000

start:
	$(COMPOSE) up -d
	@echo "Waiting for postgres to report healthy..."
	@until [ "$$($(COMPOSE) ps -q postgres | xargs docker inspect -f '{{.State.Health.Status}}')" = "healthy" ]; do \
		sleep 1; \
	done
	uv run alembic upgrade head
	uv run uvicorn kb.main:app --reload --port $(LAB_PORT)

stop:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

# Destructive: drops the named volumes.
reset:
	$(COMPOSE) down -v

# More destructive than `reset`: also kills any stray app process left
# holding $(LAB_PORT). uvicorn --reload's actual worker process can survive
# a plain `pkill -f uvicorn` (its argv doesn't match the reloader's — see
# docs/week-1.md's kill/resume section) and can even lose the "kb.main:app"
# string entirely once Python's multiprocessing re-execs it as a generic
# "spawn_main(...)" bootstrap — so this matches on the repo's own path
# (present in the worker's venv interpreter path either way) instead of any
# command-line string, and only ever targets processes rooted under this
# checkout. Use this when start/reset leave things in a weird state, not
# for routine teardown (use `reset`).
nuke:
	$(COMPOSE) down -v --remove-orphans
	@for pid in $$(lsof -ti:$(LAB_PORT) 2>/dev/null); do \
		if ps -p "$$pid" -o command= 2>/dev/null | grep -qF "$(REPO_DIR)"; then \
			echo "Killing stray process on :$(LAB_PORT) rooted in this repo: $$pid"; \
			kill -9 "$$pid"; \
		fi; \
	done

# Destructive: deletes every row from every app table (not the schema —
# migrations stay applied), without tearing down the container. Faster
# than `reset` when you just want a clean corpus mid-session. Table list
# is read from Postgres itself so this doesn't need updating as weeks add
# tables.
db-clear:
	$(COMPOSE) exec -T postgres psql -U "$${POSTGRES_USER:-substack_brain}" -d "$${POSTGRES_DB:-substack_brain}" -c \
		"DO \$$\$$ DECLARE r RECORD; BEGIN FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version') LOOP EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) || ' RESTART IDENTITY CASCADE'; END LOOP; END \$$\$$;"

lint:
	uv run ruff check .
	uv run mypy

test:
	uv run pytest

check: lint test
