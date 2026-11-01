# Production Dockerfile for The Substack Brain (Week 6)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5.29 /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini publications.yaml ./
COPY alembic ./alembic
COPY evals ./evals
COPY kb ./kb

RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["sh", "-c", "uv run python -m alembic upgrade head && uv run python -m uvicorn kb.main:app --host 0.0.0.0 --port ${LAB_PORT:-8000}"]

