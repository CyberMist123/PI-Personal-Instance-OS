from pathlib import Path

from cmx_mcp.db import Database


def test_sqlite_fts_roundtrip(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses(
        [
            {
                "id": "1",
                "account": {"id": "a", "acct": "fable"},
                "text": "hello private world",
                "spoiler_text": "",
                "created_at": "2026-07-17T00:00:00Z",
                "edited_at": None,
                "visibility": "private",
                "in_reply_to_id": None,
            }
        ]
    )
    result = db.search_statuses("private", 5)
    assert result[0]["id"] == "1"
