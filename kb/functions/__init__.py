"""The function list passed to `inngest.fast_api.serve(...)` in `kb/main.py`."""

from kb.functions.discovery import add_publication
from kb.functions.ingest import ingest_article

FUNCTIONS = [add_publication, ingest_article]
