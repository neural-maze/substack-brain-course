"""The function list passed to `inngest.fast_api.serve(...)` in `kb/main.py`.

Week 2 registers `backfill_publication` instead of week 1's `add_publication`:
both listen to `kb/publication.added`, so only one of them can be active.
"""

import inngest

from kb.functions.backfill import backfill_publication
from kb.functions.ingest import ingest_article

FUNCTIONS: list[inngest.Function] = [backfill_publication, ingest_article]
