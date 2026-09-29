"""Week 1 tests: small, fast, and no infrastructure needed.

Run them with `make test` (or `uv run pytest -v`). They don't need Docker,
Postgres, Inngest or an OpenAI key, because they only test the small pure
functions the pipeline is built from:

    URL  ->  canonical_id  ->  allowlist check  ->  chunk_text

Each test is a few lines and says what it proves in its name.
"""

import pytest

from kb.schemas.ids import canonical_id, normalize_url
from kb.sources.allowlist import PublicationEntry, assert_allowed
from kb.sources.chunking import chunk_text
from kb.sources.rss import parse_feed

# --- 1. URLs: the same article must always get the same id ------------------


def test_tracking_params_and_trailing_slash_are_removed() -> None:
    messy = "http://TheNeuralMaze.substack.com/p/my-post/?utm_source=x#top"
    assert normalize_url(messy) == "https://theneuralmaze.substack.com/p/my-post"


def test_canonical_id_is_publication_plus_post_slug() -> None:
    url = "https://theneuralmaze.substack.com/p/my-post?ref=share"
    assert canonical_id("the-neural-maze", url) == "pub:the-neural-maze:my-post"


# --- 2. Allowlist: we only ingest from publications.yaml -------------------

NEURAL_MAZE = PublicationEntry(
    slug="the-neural-maze",
    name="The Neural Maze",
    feed_url="https://theneuralmaze.substack.com/feed",
    homepage="https://theneuralmaze.substack.com",
)


def test_allowed_url_passes() -> None:
    url = "https://theneuralmaze.substack.com/p/my-post"
    assert assert_allowed(url, [NEURAL_MAZE]).slug == "the-neural-maze"


def test_unknown_site_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside the allowlist"):
        assert_allowed("https://random-blog.example.com/post", [NEURAL_MAZE])


# --- 3. Chunking: long text becomes overlapping pieces ----------------------

TEXT = "the quick brown fox jumps over the lazy dog while the cat watches"


def test_short_text_stays_in_one_chunk() -> None:
    assert chunk_text(TEXT, chunk_size=1000, chunk_overlap=10) == [TEXT]


def test_long_text_is_split_and_no_chunk_is_too_big() -> None:
    chunks = chunk_text(TEXT, chunk_size=30, chunk_overlap=10)
    assert len(chunks) > 1
    assert all(len(chunk) <= 30 for chunk in chunks)


def test_neighbouring_chunks_share_some_words() -> None:
    chunks = chunk_text(TEXT, chunk_size=30, chunk_overlap=10)
    for first, second in zip(chunks, chunks[1:], strict=False):
        assert set(first.split()) & set(second.split())


# --- 4. RSS: turn a feed into a list of articles ----------------------------

FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Demo</title>
  <item>
    <title>First post</title>
    <link>https://theneuralmaze.substack.com/p/first-post</link>
    <pubDate>Tue, 16 Sep 2025 09:48:40 GMT</pubDate>
  </item>
</channel></rss>"""


def test_feed_entries_are_parsed() -> None:
    (entry,) = parse_feed(FEED)
    assert entry.title == "First post"
    assert entry.url == "https://theneuralmaze.substack.com/p/first-post"
    assert entry.published_at.year == 2025
