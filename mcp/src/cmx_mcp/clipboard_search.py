"""Keyword search over one account's own entries.

Implemented as a scan, not an FTS5 index. Two reasons, both concrete:

1. The corpus is tiny. The 2 GiB account quota is almost entirely file bytes;
   the searchable part is text (<=10k chars per entry) plus filenames, so even a
   full account is a few megabytes. A scan is sub-millisecond and needs no index
   to keep in sync with inserts, deletes and the daily burn.
2. The content is mostly Chinese. FTS5's default unicode61 tokenizer does not
   segment CJK at all, and the trigram tokenizer cannot match queries shorter
   than three characters — which rules out ordinary two-character Chinese words
   like 烧菜. Substring matching is simply the correct semantics here.

The interface is deliberately narrow so that swapping in an index later is a
change to this file alone.
"""

from __future__ import annotations

from typing import Any, Iterable

MIN_QUERY_CHARS = 1
MAX_QUERY_CHARS = 100


def normalize_query(raw: str | None) -> str:
    query = str(raw or "").strip()
    return query[:MAX_QUERY_CHARS]


def matching_entry_ids(rows: Iterable[dict[str, Any]], query: str) -> set[str]:
    """Return entry ids whose text or any filename contains `query`.

    Case-insensitive. `rows` must already be scoped to one owner_account_id by
    the caller — this function never sees the account and cannot enforce it.
    """
    needle = query.casefold()
    if len(needle) < MIN_QUERY_CHARS:
        return set()
    hits: set[str] = set()
    for row in rows:
        haystack = f"{row.get('text') or ''}\n{row.get('names') or ''}".casefold()
        if needle in haystack:
            hits.add(str(row["entry_id"]))
    return hits
