"""Substring search across every status on this instance.

This is the Owner's search, and it is deliberately not `Database.search_statuses`.
That one answers "what has this resident already read", is scoped per bot, and
excludes `direct`/`self` on purpose. The Owner's question is the opposite one —
anything on this instance should be findable, including their own private diary.

Mastodon's own web search cannot answer it either: full text needs Elasticsearch,
`ES_ENABLED=false` here, and even with it Mastodon indexes only your own posts and
ones you interacted with. So this reads PostgreSQL, which already holds every
status, and greps it.

Reaching PostgreSQL crosses a boundary AGENTS.md draws for AI and MCP. It is drawn
there because a direct connection bypasses Mastodon's visibility rules entirely, so
a resident holding one could read every DM on the instance. Nothing here is
reachable by a resident: the caller must be the Owner, checked by account and not
merely by holding a valid token. See issue #31.

psql runs through an argument list with no shell, and the term is bound with
`:'term'`, which quotes it as a SQL literal. LIKE wildcards are escaped here so a
query for `100%` looks for those four characters instead of matching everything.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

DB_CONTAINER = "pi-os-db-1"
DB_USER = "mastodon"
DB_NAME = "mastodon_production"
QUERY_TIMEOUT_SECONDS = 30
MAX_LIMIT = 100

# Mastodon's visibility enum, which the API renders as words and the column stores
# as integers.
VISIBILITY_NAMES = {0: "public", 1: "unlisted", 2: "private", 3: "direct"}

_SQL = """
SELECT COALESCE(json_agg(row_to_json(hit) ORDER BY hit.created_at DESC), '[]')
FROM (
    SELECT s.id::text AS id,
           s.created_at,
           s.visibility,
           a.username AS author,
           s.text
    FROM statuses s
    JOIN accounts a ON a.id = s.account_id
    WHERE s.deleted_at IS NULL
      AND s.text ILIKE '%' || :'term' || '%' ESCAPE '\\'
    ORDER BY s.created_at DESC
    LIMIT :limit
) hit
"""


def escape_like(value: str) -> str:
    """Neutralise LIKE wildcards so `100%` looks for those four characters."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_site(query: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """Return statuses whose text contains `query`, newest first.

    Raises RuntimeError if psql could not be reached or returned an error; an
    empty result is a plain empty list.
    """
    term = query.strip()
    if not term:
        return []
    bounded = max(1, min(int(limit), MAX_LIMIT))
    # The statement arrives on stdin rather than through -c: psql only performs
    # `:'term'` interpolation while lexing its input, and -c hands the string
    # straight to the server, where the placeholder is a syntax error.
    command = [
        "docker", "exec", "-i", DB_CONTAINER,
        "psql", "-U", DB_USER, "-d", DB_NAME, "-tA",
        "-v", "ON_ERROR_STOP=1",
        "-v", f"term={escape_like(term)}",
        "-v", f"limit={bounded}",
    ]
    try:
        completed = subprocess.run(
            command, input=_SQL, capture_output=True, text=True, encoding="utf-8",
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"site search could not run: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "psql failed").strip()[:300])
    payload = (completed.stdout or "").strip()
    if not payload:
        return []
    try:
        rows = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError("site search returned an unreadable result") from exc
    return [_shape(row) for row in rows]


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "at": row.get("created_at"),
        "author": row.get("author") or "",
        "visibility": VISIBILITY_NAMES.get(row.get("visibility"), str(row.get("visibility"))),
        "text": (row.get("text") or "").strip(),
    }
