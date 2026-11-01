"""Week 6 unit tests: Inngest functions (`run-research-agent`, `generate-weekly-digest`),
event schemas, `research_id()` generator, MCP tool registration (`kb_research`,
`kb_research_result`, `kb_weekly_digest`), and production deployment files.

Runs with zero Docker containers or external API keys.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

from kb.config import Settings, settings
from kb.functions import FUNCTIONS
from kb.functions.digest import generate_weekly_digest
from kb.functions.research import run_research_agent
from kb.mcp.server import mcp
from kb.schemas.events import (
    DIGEST_PUBLISHED,
    DIGEST_REQUESTED,
    RESEARCH_COMPLETED,
    RESEARCH_REQUESTED,
    DigestPublished,
    DigestRequested,
    ResearchCompleted,
    ResearchRequested,
)
from kb.schemas.ids import research_id


def test_week6_functions_are_registered_in_functions_list() -> None:
    fn_ids = {fn.get_config("http://localhost:8000").main.id for fn in FUNCTIONS}
    assert "substack-brain-run-research-agent" in fn_ids
    assert "substack-brain-generate-weekly-digest" in fn_ids
    assert len(FUNCTIONS) == 9


def test_run_research_agent_trigger_and_concurrency() -> None:
    config = run_research_agent.get_config("http://localhost:8000").main
    event_names = {getattr(t, "event", None) for t in config.triggers}
    assert event_names == {RESEARCH_REQUESTED}
    assert config.concurrency is not None
    assert config.concurrency[0].limit == settings.concurrency_research_runs


def test_generate_weekly_digest_triggers_on_cron_and_event() -> None:
    config = generate_weekly_digest.get_config("http://localhost:8000").main
    triggers = config.triggers
    assert len(triggers) == 2
    cron_triggers = [t for t in triggers if hasattr(t, "cron")]
    event_triggers = [t for t in triggers if hasattr(t, "event")]
    assert any(t.cron == "0 8 * * 1" for t in cron_triggers)
    assert any(t.event == DIGEST_REQUESTED for t in event_triggers)

    # Verify Makefile-exported quoted cron strings are stripped before Inngest registration
    s = Settings(_env_file=None, digest_cron='"0 8 * * 1"')  # type: ignore[call-arg]
    assert s.digest_cron == "0 8 * * 1"


def test_research_ids_are_prefixed_and_unique() -> None:
    ids = {research_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("res_") and len(i) == 16 for i in ids)


def test_week6_event_schemas() -> None:
    req = ResearchRequested(
        research_id="res_abc123456789",
        question="How does 3-way RRF compare to 2-way RRF?",
        max_iterations=3,
        max_usd=0.25,
    )
    assert req.model_dump(mode="json") == {
        "research_id": "res_abc123456789",
        "question": "How does 3-way RRF compare to 2-way RRF?",
        "max_iterations": 3,
        "max_usd": 0.25,
    }

    comp = ResearchCompleted(
        research_id="res_abc123456789",
        status="completed",
        iterations_used=2,
        total_cost_usd=0.0142,
        confidence=1.0,
        failure_codes=[],
    )
    assert comp.model_dump(mode="json")["status"] == "completed"
    assert RESEARCH_COMPLETED == "kb/research.completed"

    d_req = DigestRequested(lookback_days=7, publication_slugs=["the-neural-maze"])
    assert d_req.model_dump(mode="json")["lookback_days"] == 7

    did = uuid.uuid4()
    now = datetime.now(UTC)
    d_pub = DigestPublished(
        digest_id=did,
        window_start=now,
        window_end=now,
        article_count=5,
        claim_count=18,
        disagreements_count=2,
    )
    assert d_pub.model_dump(mode="json")["digest_id"] == str(did)
    assert DIGEST_PUBLISHED == "kb/digest.published"


def test_mcp_server_registers_all_thirteen_tools() -> None:
    tools_dict = mcp._tool_manager._tools
    assert "kb_research" in tools_dict
    assert "kb_research_result" in tools_dict
    assert "kb_weekly_digest" in tools_dict
    assert len(tools_dict) == 13


def test_production_deployment_files_exist_and_are_valid() -> None:
    dockerfile = Path("Dockerfile")
    assert dockerfile.exists()
    content = dockerfile.read_text(encoding="utf-8")
    assert "alembic upgrade head" in content
    assert "uvicorn kb.main:app" in content

    prod_compose = Path("infra/docker-compose.prod.yml")
    assert prod_compose.exists()
    compose_data = yaml.safe_load(prod_compose.read_text(encoding="utf-8"))
    services = compose_data["services"]
    assert {"postgres", "memgraph", "kb-app"} <= set(services.keys())
    assert services["kb-app"]["environment"]["INNGEST_DEV"] == "false"

