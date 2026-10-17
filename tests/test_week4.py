"""Week 4 unit tests: `extract-claims` function config, Cypher invariants, agent
layer, and `ClaimsExtracted` event contract.

Runs with zero Docker, database, or external API dependencies.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from kb.agents.core import Agent, run_agent
from kb.functions import FUNCTIONS
from kb.functions.enrich import extract_claims
from kb.graph import cypher
from kb.schemas.entities import ClaimExtractionBatch, RawExtractedClaim
from kb.schemas.events import ARTICLE_INGESTED, ARTICLE_UPDATED, ClaimsExtracted


def test_extract_claims_is_registered_in_functions_list() -> None:
    fn_ids = {fn.get_config("http://localhost:8000").main.id for fn in FUNCTIONS}
    assert "substack-brain-extract-claims" in fn_ids


def test_extract_claims_triggers_on_ingested_and_updated_events() -> None:
    config = extract_claims.get_config("http://localhost:8000").main
    event_names = {getattr(t, "event", None) for t in config.triggers}
    assert event_names == {ARTICLE_INGESTED, ARTICLE_UPDATED}


def test_extract_claims_concurrency_serializes_per_canonical_id() -> None:
    config = extract_claims.get_config("http://localhost:8000").main
    assert config.concurrency is not None
    keys = [c.key for c in config.concurrency]
    assert "event.data.canonical_id" in keys
    per_article = next(c for c in config.concurrency if c.key == "event.data.canonical_id")
    assert per_article.limit == 1


def test_cypher_module_obeys_parameterization_merge_and_valid_to_invariants() -> None:
    cypher_statements = {
        name: getattr(cypher, name)
        for name in dir(cypher)
        if name.endswith("_CYPHER") and isinstance(getattr(cypher, name), str)
    }
    assert len(cypher_statements) >= 7

    for name, stmt in cypher_statements.items():
        # Rule 1: Every query taking inputs must use $parameter placeholders
        if name != "COUNT_ACTIVE_CLAIMS_AND_TOPICS_CYPHER":
            assert "$" in stmt
        # Rule 2: Never standalone CREATE (only ON CREATE SET inside MERGE)
        without_on_create = re.sub(r"ON\s+CREATE\s+SET", "", stmt, flags=re.IGNORECASE)
        assert not re.search(r"\bCREATE\b", without_on_create, re.IGNORECASE)

    # Rule 3: Every read/traversal query must filter valid_to IS NULL
    for read_stmt in (
        cypher.SEARCH_CLAIMS_BY_TOPICS_CYPHER,
        cypher.GET_CLAIM_PROVENANCE_CYPHER,
        cypher.COUNT_ACTIVE_CLAIMS_AND_TOPICS_CYPHER,
        cypher.CROSS_PUBLICATION_DISAGREEMENTS_CYPHER,
    ):
        assert "valid_to IS NULL" in read_stmt


def test_claims_extracted_event_schema() -> None:
    aid = uuid.uuid4()
    evt = ClaimsExtracted(
        article_id=aid,
        canonical_id="pub:the-neural-maze:graph-rag",
        claim_count=4,
        dropped_spans=1,
    )
    dumped = evt.model_dump(mode="json")
    assert dumped["article_id"] == str(aid)
    assert dumped["canonical_id"] == "pub:the-neural-maze:graph-rag"
    assert dumped["claim_count"] == 4
    assert dumped["dropped_spans"] == 1


async def test_run_agent_wraps_call_in_inngest_step_and_validates_schema() -> None:
    recorded_step_ids: list[str] = []

    class DummyStep:
        async def run(self, step_id: str, fn: Any) -> Any:
            recorded_step_ids.append(step_id)
            return {
                "output": {
                    "claims": [
                        {
                            "claim_text": "Reciprocal Rank Fusion needs no score calibration.",
                            "quote_span": (
                                "Reciprocal Rank Fusion combines rankings "
                                "using only ordinal positions."
                            ),
                            "claim_type": "opinion",
                            "entities": ["Reciprocal Rank Fusion"],
                            "confidence": 0.94,
                        }
                    ]
                },
                "usage": {
                    "model": "claude-sonnet-4-20250514",
                    "input_tokens": 120,
                    "output_tokens": 45,
                    "est_usd": 0.001035,
                },
            }

    test_agent = Agent(
        name="test-extractor",
        role="Test Extractor",
        system_prompt="Extract claims.",
        model="claude-sonnet-4-20250514",
        output_schema=ClaimExtractionBatch,
    )

    result = await run_agent(
        step=DummyStep(),
        agent=test_agent,
        user_input="Article text",
        step_id="extract-claims-llm",
    )

    assert recorded_step_ids == ["extract-claims-llm"]
    assert isinstance(result, ClaimExtractionBatch)
    assert len(result.claims) == 1
    assert isinstance(result.claims[0], RawExtractedClaim)
    assert result.claims[0].claim_type == "opinion"
