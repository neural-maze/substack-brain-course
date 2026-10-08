"""Week 2 tests: same rules as test_basics.py, no Docker, database or API key.

They cover the parts of week 2 that are pure logic or pure configuration:

    RRF fusion  ->  ingest-article's flow control  ->  allowlist & ids for 3 publications
"""

from kb.functions.ingest import ingest_article
from kb.retrieval.hybrid import compute_rrf
from kb.schemas.ids import canonical_id, snapshot_id
from kb.sources.allowlist import find_publication_for_url, load_publications


def _passage(pid: str) -> dict[str, object]:
    return {
        "passage_id": pid,
        "text": f"text of {pid}",
        "title": "A title",
        "author": None,
        "url": f"https://example.com/p/{pid}",
        "published_at": "2026-09-30T00:00:00+00:00",
        "rank": 0.5,
    }


# --- 1. RRF: positions, not scores ------------------------------------------


def test_agreement_between_engines_beats_one_engines_favourite() -> None:
    # A: keyword #1, vector #2. C: keyword #3, vector #5. B: only vector #1.
    sparse = [_passage("A"), _passage("X"), _passage("C")]
    dense = [_passage("B"), _passage("A"), _passage("Y"), _passage("Z"), _passage("C")]
    fused = compute_rrf(sparse, dense, k=60)
    order = [c.passage_id for c in fused]
    assert order.index("A") < order.index("C") < order.index("B")


def test_rrf_score_is_the_sum_of_one_over_k_plus_rank() -> None:
    fused = {c.passage_id: c for c in compute_rrf([_passage("A")], [_passage("B"), _passage("A")])}
    assert abs(fused["A"].rrf_score - (1 / 61 + 1 / 62)) < 1e-12
    assert fused["A"].source == "both"
    assert (fused["A"].sparse_rank, fused["A"].dense_rank) == (1, 2)
    assert fused["B"].source == "vector"


def test_ties_are_broken_by_passage_id_so_results_are_deterministic() -> None:
    fused = compute_rrf([_passage("b")], [_passage("a")])
    assert [c.passage_id for c in fused] == ["a", "b"]


# --- 2. Flow control lives on ingest-article itself -------------------------


def _config():  # type: ignore[no-untyped-def]
    return ingest_article.get_config("http://localhost:8000").main


def test_concurrency_is_keyed_per_publication() -> None:
    (concurrency,) = _config().concurrency
    assert concurrency.key == "event.data.publication_id"


def test_runs_are_throttled_globally() -> None:
    throttle = _config().throttle
    assert throttle is not None
    assert throttle.key is None  # one OpenAI quota, shared by every publication


def test_manual_runs_are_shifted_ahead_and_backfill_runs_behind() -> None:
    priority = _config().priority
    assert priority is not None
    assert priority.run == "event.data.via == 'manual' ? 600 : -600"


def test_no_idempotency_key_so_week_1_retries_still_work() -> None:
    assert _config().idempotency is None


# --- 3. Three publications, one id per article ------------------------------


def test_substack_and_custom_domain_urls_belong_to_the_same_publication() -> None:
    pubs = load_publications()
    for url in (
        "https://www.theneuralmaze.com/p/some-post",
        "https://theneuralmaze.substack.com/p/some-post",
    ):
        pub = find_publication_for_url(url, pubs)
        assert pub is not None and pub.slug == "the-neural-maze"


def test_the_same_post_gets_the_same_id_on_either_domain() -> None:
    a = canonical_id("the-neural-maze", "https://www.theneuralmaze.com/p/some-post/")
    b = canonical_id("the-neural-maze", "https://theneuralmaze.substack.com/p/some-post?ref=x")
    assert a == b == "pub:the-neural-maze:some-post"


def test_snapshot_ids_are_prefixed_and_unique() -> None:
    ids = {snapshot_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("snap_") and len(i) == 17 for i in ids)
