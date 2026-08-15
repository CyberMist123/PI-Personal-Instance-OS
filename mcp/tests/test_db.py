from pathlib import Path

from cmx_mcp.db import Database


def test_sqlite_fts_roundtrip(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses(
        "gpt",
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
    result = db.search_statuses("gpt", "private", 5)
    assert result[0]["id"] == "1"


def test_search_matches_chinese_substrings_not_only_whole_sentences(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses(
        "gpt",
        [
            {"id": "1", "account": {"acct": "a"}, "text": "今天烧了个菜很好吃", "created_at": "2026-07-17T00:00:00Z"},
            {"id": "2", "account": {"acct": "a"}, "text": "学习烧菜的第一天", "created_at": "2026-07-18T00:00:00Z"},
            {"id": "3", "account": {"acct": "a"}, "text": "读了一本书", "created_at": "2026-07-19T00:00:00Z"},
        ],
    )
    assert [item["id"] for item in db.search_statuses("gpt", "烧菜", 5)] == ["2"]
    # Newest first, and a single character is a legitimate Chinese query.
    assert [item["id"] for item in db.search_statuses("gpt", "烧", 5)] == ["2", "1"]
    assert db.search_statuses("gpt", "游泳", 5) == []


def test_chinese_substring_search_survives_an_fts_zero_result(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses("gpt", [{"id": "1", "account": {"acct": "a"}, "text": "今天去Commonwealth修自行车然后买了饮料"}])
    with db.connect() as raw:
        assert raw.execute("SELECT status_id FROM status_fts WHERE status_fts MATCH ?", ("修自行",)).fetchall() == []
    assert [item["id"] for item in db.search_statuses("gpt", "修自行", 5)] == ["1"]
    assert [item["id"] for item in db.search_statuses("gpt", "Common", 5)] == ["1"]


def test_search_never_reaches_direct_or_self_entries(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses(
        "gpt",
        [
            {"id": "d", "account": {"id": "other", "acct": "a"}, "text": "烧菜的秘密", "visibility": "direct"},
            {"id": "p", "account": {"acct": "a"}, "text": "烧菜的公开笔记", "visibility": "private"},
        ],
    )
    assert [item["id"] for item in db.search_statuses("gpt", "烧菜", 5)] == ["p"]


def test_search_includes_only_the_current_residents_own_direct_diary(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses("gpt", [
        {"id": "mine", "account": {"id": "self", "acct": "gpt"}, "text": "自己的秘密日记", "visibility": "direct"},
        {"id": "other", "account": {"id": "other", "acct": "alice"}, "text": "别人的秘密日记", "visibility": "direct"},
    ])
    assert [item["id"] for item in db.search_statuses("gpt", "秘密日记", 5, self_author_id="self")] == ["mine"]
    assert db.search_statuses("gpt", "自己的秘密日记", 5) == []


def test_search_treats_like_wildcards_as_literal_characters(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses(
        "gpt",
        [
            {"id": "1", "account": {"acct": "a"}, "text": "电池还剩 50% 电量"},
            {"id": "2", "account": {"acct": "a"}, "text": "完全无关的一条"},
        ],
    )
    assert [item["id"] for item in db.search_statuses("gpt", "50%", 5)] == ["1"]
    # Unescaped, a bare "%" would match every row; escaped, it finds the literal sign.
    assert [item["id"] for item in db.search_statuses("gpt", "%", 5)] == ["1"]


def test_later_cloud_pass_does_not_blank_an_earlier_one(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.record_local_ocr("abc123", text="菜单", line_count=1, mean_confidence=0.9)
    db.record_cloud_recognition("abc123", corrected_text="今日菜单")
    db.record_cloud_recognition("abc123", description="一张手写餐牌")
    row = db.get_image_recognition("abc123")
    assert row["cloud_corrected_text"] == "今日菜单"
    assert row["cloud_description"] == "一张手写餐牌"


def test_search_finds_a_status_by_text_recognised_inside_its_image(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses("gpt", [{"id": "s1", "account": {"acct": "a"}, "text": "今天做了这个"}])
    db.record_local_ocr("sha-menu", text="蜂蜜柠檬脆皮鸡翅", line_count=1, mean_confidence=0.95)
    db.link_status_media("s1", "m1", "sha-menu")
    assert [item["id"] for item in db.search_statuses("gpt", "鸡翅", 5)] == ["s1"]
    assert db.search_statuses("gpt", "红烧肉", 5) == []


def test_search_finds_media_alt_and_voice_transcript_alt(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses("gpt", [
        {"id": "image", "account": {"acct": "a"}, "text": "照片", "media": [{"description": "图片里写着 Commonwealth 单车维修"}]},
        {"id": "voice", "account": {"acct": "a"}, "text": "明早去海边跑步", "media": [{"description": "明早去海边跑步"}]},
    ])
    assert [item["id"] for item in db.search_statuses("gpt", "单车维修", 5)] == ["image"]
    assert [item["id"] for item in db.search_statuses("gpt", "海边跑步", 5)] == ["voice"]


def test_search_fuzzy_chinese_and_pinyin_cover_status_media_ocr_and_voice(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses("gpt", [
        {"id": "sweep", "account": {"acct": "a"}, "text": "今天做了一次大扫除", "created_at": "2026-08-10T00:00:00Z"},
        {"id": "pasta", "account": {"acct": "a"}, "text": "晚餐吃意大利面", "created_at": "2026-08-09T00:00:00Z"},
        {"id": "image", "account": {"acct": "a"}, "text": "照片", "media": [{"description": "图片里的青柠汽水"}]},
        {"id": "voice", "account": {"acct": "a"}, "text": "语音转写：明早去海边跑步"},
    ])
    db.record_local_ocr("sha-menu", text="蜂蜜柠檬脆皮鸡翅", line_count=1, mean_confidence=0.9)
    db.link_status_media("image", "m1", "sha-menu")

    assert [item["id"] for item in db.search_statuses("gpt", "大扫除", 1)] == ["sweep"]
    assert [item["id"] for item in db.search_statuses("gpt", "大扫厨", 1)] == ["sweep"]
    assert [item["id"] for item in db.search_statuses("gpt", "意大力面", 1)] == ["pasta"]
    assert [item["id"] for item in db.search_statuses("gpt", "dasaochu", 1)] == ["sweep"]
    assert [item["id"] for item in db.search_statuses("gpt", "dasaocu", 1)] == ["sweep"]
    assert [item["id"] for item in db.search_statuses("gpt", "dsc", 1)] == ["sweep"]
    assert [item["id"] for item in db.search_statuses("gpt", "qingningqishui", 1)] == ["image"]
    assert [item["id"] for item in db.search_statuses("gpt", "haibianpaobu", 1)] == ["voice"]
    assert [item["id"] for item in db.search_statuses("gpt", "fengminingmengcuipijichi", 1)] == ["image"]


def test_search_keeps_exact_before_fuzzy_deduplicates_and_filters_direct(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses("gpt", [
        {"id": "exact", "account": {"acct": "a"}, "text": "大扫厨的错字记录", "created_at": "2026-08-10T00:00:00Z"},
        {"id": "fuzzy", "account": {"acct": "a"}, "text": "今天做了一次大扫除", "created_at": "2026-08-09T00:00:00Z"},
        {"id": "hidden", "account": {"id": "other", "acct": "b"}, "text": "今天做了一次大扫除", "visibility": "direct"},
    ])
    db.record_local_ocr("sha-1", text="大扫除", line_count=1, mean_confidence=0.9)
    db.record_local_ocr("sha-2", text="大扫除", line_count=1, mean_confidence=0.9)
    db.link_status_media("fuzzy", "m1", "sha-1")
    db.link_status_media("fuzzy", "m2", "sha-2")

    assert [item["id"] for item in db.search_statuses("gpt", "大扫厨", 5)] == ["exact", "fuzzy"]
    assert [item["id"] for item in db.search_statuses("gpt", "dasaochu", 5)] == ["exact", "fuzzy"]


def test_image_text_cannot_pull_a_direct_status_into_results(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses("gpt", [{"id": "d1", "account": {"acct": "a"}, "text": "私密", "visibility": "direct"}])
    db.record_local_ocr("sha-secret", text="蜂蜜柠檬脆皮鸡翅", line_count=1, mean_confidence=0.95)
    db.link_status_media("d1", "m1", "sha-secret")
    assert db.search_statuses("gpt", "鸡翅", 5) == []


def test_one_recognition_serves_every_status_that_reuses_the_image(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.record_local_ocr("sha-same", text="同一张图", line_count=1, mean_confidence=0.9)
    db.link_status_media("s1", "m1", "sha-same")
    db.link_status_media("s2", "m9", "sha-same")
    assert db.recognitions_for_status("s1")["m1"]["local_ocr_text"] == "同一张图"
    assert db.recognitions_for_status("s2")["m9"]["local_ocr_text"] == "同一张图"


def test_status_cache_isolated_by_bot_id(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    status = {"id": "same", "account": {"acct": "a"}, "text": "private", "spoiler_text": ""}
    db.cache_statuses("a", [status])
    db.cache_statuses("b", [{**status, "text": "other"}])
    assert db.search_statuses("a", "private", 5)[0]["text"] == "private"
    assert db.search_statuses("b", "private", 5) == []


def test_upsert_bot_round_trips_remote_profile_and_capabilities(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    common = {
        "bot_id": "social-bot",
        "display_name": "Social Bot",
        "profile": "resident",
        "media_root": tmp_path / "media",
        "token_ref": "social.token",
        "default_audience": "residents",
        "allow_public": False,
    }
    db.upsert_bot(
        **common,
        remote_profile="social",
        remote_polls=True,
        remote_boosts=False,
        remote_notifications=True,
    )
    created = db.get_bot("social-bot")
    assert created.remote_profile == "social"
    assert created.remote_polls is True
    assert created.remote_boosts is False
    assert created.remote_notifications is True
    assert [(bot.bot_id, bot.remote_profile) for bot in db.list_bots()] == [("social-bot", "social")]

    db.upsert_bot(
        **{**common, "display_name": "Social Plus Bot"},
        remote_profile="social_plus",
        remote_polls=False,
        remote_boosts=True,
        remote_notifications=True,
    )
    updated = db.get_bot("social-bot")
    assert updated.display_name == "Social Plus Bot"
    assert updated.remote_profile == "social_plus"
    assert updated.remote_polls is False
    assert updated.remote_boosts is True
    assert updated.remote_notifications is True
    listed = db.list_bots()
    assert len(listed) == 1
    assert listed[0] == updated


def test_legacy_cache_migrates_without_losing_single_bot_rows(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    import sqlite3

    with sqlite3.connect(path) as raw:
        raw.executescript("""
            CREATE TABLE bots (bot_id TEXT PRIMARY KEY, display_name TEXT, profile TEXT,
                media_root TEXT, token_ref TEXT, default_audience TEXT, allow_public INTEGER,
                enabled INTEGER, created_at INTEGER, updated_at INTEGER);
            INSERT INTO bots VALUES ('gpt','GPT','reader','.', 'token','residents',0,1,1,1);
            CREATE TABLE status_cache (status_id TEXT PRIMARY KEY, author_id TEXT NOT NULL,
                author_acct TEXT NOT NULL, text TEXT NOT NULL, spoiler_text TEXT NOT NULL DEFAULT '',
                created_at TEXT, edited_at TEXT, visibility TEXT, reply_to_id TEXT,
                payload_json TEXT NOT NULL, indexed_at INTEGER NOT NULL);
            INSERT INTO status_cache VALUES ('same','a','acct','legacy','',NULL,NULL,NULL,NULL,'{"id":"same","text":"legacy"}',1);
            CREATE VIRTUAL TABLE status_fts USING fts5(status_id UNINDEXED, author_acct, text, spoiler_text);
        """)
    db = Database(path)
    db.initialize()
    assert db.search_statuses("gpt", "legacy", 5)[0]["id"] == "same"


def test_dedup_claim_is_atomic_and_recoverable(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    first = db.claim_dedup(bot_id="a", operation="fake", request_id="r", lease_seconds=60)
    second = db.claim_dedup(bot_id="a", operation="fake", request_id="r", lease_seconds=60)
    assert first["claimed"] is True
    assert second["claimed"] is False
    db.finish_dedup(bot_id="a", operation="fake", request_id="r", response={"id": "1"})
    done = db.claim_dedup(bot_id="a", operation="fake", request_id="r")
    assert done["state"] == "succeeded"
    assert done["response"] == {"id": "1"}
    other_bot = db.claim_dedup(bot_id="b", operation="fake", request_id="r")
    assert other_bot["claimed"] is True


def test_direct_statuses_are_cached_but_not_indexed(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.cache_statuses("gpt", [{"id": "d", "account": {"acct": "a"}, "text": "secret", "visibility": "direct"}])
    assert db.search_statuses("gpt", "secret", 5) == []


def test_browse_schema_v3_and_bot_isolation(tmp_path: Path):
    import sqlite3
    path = tmp_path / "cmx.sqlite3"; db = Database(path); db.initialize()
    assert db.commit_browse(bot_id="a", feed="timeline", expected_watermark=None, watermark="10", seen_ids=["source"], visit_id="va", allowed_ids=["source"], max_open=2, char_budget_limit=5000, char_budget_used=100, expires_at=9999999999)
    assert db.commit_browse(bot_id="b", feed="timeline", expected_watermark=None, watermark="20", seen_ids=[], visit_id="vb", allowed_ids=["other"], max_open=3, char_budget_limit=5000, char_budget_used=100, expires_at=9999999999)
    assert db.get_browse_watermark("a") == "10"
    assert db.seen_status_ids("a", ["source"]) == {"source"}
    assert db.seen_status_ids("b", ["source"]) == set()
    assert db.get_visit("a", "vb") is None
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 8


def test_visit_rejects_repeat_and_budget_overrun(tmp_path: Path):
    import pytest
    db = Database(tmp_path / "cmx.sqlite3"); db.initialize()
    db.commit_browse(bot_id="a", feed="timeline", expected_watermark=None, watermark="1", seen_ids=[], visit_id="v", allowed_ids=["1", "2", "3"], max_open=2, char_budget_limit=120, char_budget_used=10, expires_at=9999999999)
    assert db.use_visit(bot_id="a", visit_id="v", opened_ids=["1"], added_chars=10)
    with pytest.raises(ValueError, match="reopened"):
        db.use_visit(bot_id="a", visit_id="v", opened_ids=["1"], added_chars=1)
    assert db.use_visit(bot_id="a", visit_id="v", opened_ids=["2"], added_chars=101) is False
    with pytest.raises(ValueError, match="at most 2"):
        db.use_visit(bot_id="a", visit_id="v", opened_ids=["2", "3"], added_chars=1)


def test_filebox_quota_and_settings_roundtrip(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.filebox_add(bot_id="gpt", file_id="f1", file_name="a.zip", size_bytes=100)
    db.filebox_add(bot_id="gpt", file_id="f2", file_name="b.bin", size_bytes=50)
    db.filebox_add(bot_id="_owner", file_id="f3", file_name="c.mp4", size_bytes=999)
    assert db.filebox_usage("gpt") == 150
    assert db.filebox_get("gpt", "f1")["file_name"] == "a.zip"
    assert db.filebox_get("gpt", "missing") is None
    assert len(db.filebox_list("gpt")) == 2 and len(db.filebox_list()) == 3
    removed = db.filebox_remove("gpt", "f1")
    assert removed["size_bytes"] == 100 and db.filebox_usage("gpt") == 50
    assert db.get_setting("filebox_pass") is None
    db.set_setting("filebox_pass", "pbkdf2$aa$bb")
    assert db.get_setting("filebox_pass") == "pbkdf2$aa$bb"


def test_browse_watermark_compare_and_swap(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3"); db.initialize()
    common = dict(bot_id="a", feed="timeline", seen_ids=[], allowed_ids=[], max_open=3,
                  char_budget_limit=5000, char_budget_used=100, expires_at=9999999999)
    assert db.commit_browse(expected_watermark=None, watermark="10", visit_id="v1", **common)
    assert db.commit_browse(expected_watermark=None, watermark="20", visit_id="v2", **common) is False
    assert db.get_browse_watermark("a") == "10"


def test_real_v2_database_migrates_to_v3_without_data_loss(tmp_path: Path):
    import sqlite3
    path = tmp_path / "v2.sqlite3"
    with sqlite3.connect(path) as raw:
        raw.executescript("""
            CREATE TABLE schema_version(version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES(2);
            CREATE TABLE bots(bot_id TEXT PRIMARY KEY, display_name TEXT, profile TEXT,
                media_root TEXT, token_ref TEXT, default_audience TEXT, allow_public INTEGER,
                enabled INTEGER, created_at INTEGER, updated_at INTEGER, remote_profile TEXT,
                remote_polls INTEGER, remote_boosts INTEGER, remote_notifications INTEGER);
            INSERT INTO bots VALUES('gpt','GPT','reader','.','token','residents',0,1,1,1,'reader',1,0,0);
            CREATE TABLE status_cache(bot_id TEXT, status_id TEXT, author_id TEXT, author_acct TEXT,
                text TEXT, spoiler_text TEXT, created_at TEXT, edited_at TEXT, visibility TEXT,
                reply_to_id TEXT, payload_json TEXT, indexed_at INTEGER, PRIMARY KEY(bot_id,status_id));
            INSERT INTO status_cache VALUES('gpt','s1','a','alice','kept','',NULL,NULL,'private',NULL,'{"id":"s1"}',1);
            CREATE VIRTUAL TABLE status_fts USING fts5(bot_id UNINDEXED,status_id UNINDEXED,author_acct,text,spoiler_text);
            INSERT INTO status_fts VALUES('gpt','s1','alice','kept','');
            CREATE TABLE publish_dedup(bot_id TEXT,operation TEXT,request_id TEXT,state TEXT,status_id TEXT,
                error_code TEXT,lease_expires_at INTEGER,created_at INTEGER,updated_at INTEGER,response_json TEXT,
                PRIMARY KEY(bot_id,operation,request_id));
            INSERT INTO publish_dedup VALUES('gpt','publish','r1','succeeded','s1',NULL,NULL,1,1,'{"id":"s1"}');
            CREATE TABLE mcp_oauth_tokens(token_hash TEXT PRIMARY KEY, subject TEXT, payload TEXT);
            INSERT INTO mcp_oauth_tokens VALUES('hash','gpt','kept-oauth');
        """)
    Database(path).initialize()
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 8
        assert raw.execute("SELECT display_name FROM bots WHERE bot_id='gpt'").fetchone()[0] == "GPT"
        assert raw.execute("SELECT text FROM status_cache WHERE status_id='s1'").fetchone()[0] == "kept"
        assert raw.execute("SELECT response_json FROM publish_dedup WHERE request_id='r1'").fetchone()[0] == '{"id":"s1"}'
        assert raw.execute("SELECT payload FROM mcp_oauth_tokens WHERE token_hash='hash'").fetchone()[0] == "kept-oauth"
        tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"browse_state", "browse_seen", "browse_visits"}.issubset(tables)


def test_future_schema_version_fails_closed(tmp_path: Path):
    import sqlite3
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        raw.execute("INSERT INTO schema_version VALUES(9)")
    import pytest
    with pytest.raises(RuntimeError, match="future database schema version"):
        Database(path).initialize()
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 9


def test_real_v5_database_migrates_to_v6_without_data_loss(tmp_path: Path):
    import sqlite3
    path = tmp_path / "v5.sqlite3"
    with sqlite3.connect(path) as raw:
        raw.executescript("""
            CREATE TABLE schema_version(version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES(5);
            CREATE TABLE bots(bot_id TEXT PRIMARY KEY, display_name TEXT, profile TEXT,
                media_root TEXT, token_ref TEXT, default_audience TEXT, allow_public INTEGER,
                enabled INTEGER, created_at INTEGER, updated_at INTEGER, remote_profile TEXT,
                remote_polls INTEGER, remote_boosts INTEGER, remote_notifications INTEGER);
            INSERT INTO bots VALUES('gpt','GPT','reader','.','token','residents',0,1,1,1,'reader',1,0,0);
            CREATE TABLE status_cache(bot_id TEXT, status_id TEXT, author_id TEXT, author_acct TEXT,
                text TEXT, spoiler_text TEXT, created_at TEXT, edited_at TEXT, visibility TEXT,
                reply_to_id TEXT, payload_json TEXT, indexed_at INTEGER, PRIMARY KEY(bot_id,status_id));
            INSERT INTO status_cache VALUES('gpt','s1','a','alice','kept','',NULL,NULL,'private',NULL,'{"id":"s1"}',1);
            CREATE VIRTUAL TABLE status_fts USING fts5(bot_id UNINDEXED,status_id UNINDEXED,author_acct,text,spoiler_text);
            INSERT INTO status_fts VALUES('gpt','s1','alice','kept','');
            CREATE TABLE publish_dedup(bot_id TEXT,operation TEXT,request_id TEXT,state TEXT,status_id TEXT,
                error_code TEXT,lease_expires_at INTEGER,created_at INTEGER,updated_at INTEGER,response_json TEXT,
                PRIMARY KEY(bot_id,operation,request_id));
            INSERT INTO publish_dedup VALUES('gpt','publish','r1','succeeded','s1',NULL,NULL,1,1,'{"id":"s1"}');
            CREATE TABLE worker_done(bot_id TEXT, status_id TEXT, done_at INTEGER, PRIMARY KEY(bot_id,status_id));
            INSERT INTO worker_done VALUES('gpt','s1',1);
            CREATE TABLE filebox_files(bot_id TEXT, file_id TEXT, file_name TEXT, size_bytes INTEGER,
                created_at INTEGER, PRIMARY KEY(bot_id,file_id));
            INSERT INTO filebox_files VALUES('gpt','f1','kept.zip',10,1);
            CREATE TABLE cmx_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO cmx_settings VALUES('filebox_pass','kept-hash');
        """)
    Database(path).initialize()
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 8
        assert raw.execute("SELECT display_name FROM bots WHERE bot_id='gpt'").fetchone()[0] == "GPT"
        assert raw.execute("SELECT text FROM status_cache WHERE status_id='s1'").fetchone()[0] == "kept"
        assert raw.execute("SELECT response_json FROM publish_dedup WHERE request_id='r1'").fetchone()[0] == '{"id":"s1"}'
        assert raw.execute("SELECT done_at FROM worker_done WHERE status_id='s1'").fetchone()[0] == 1
        assert raw.execute("SELECT file_name FROM filebox_files WHERE file_id='f1'").fetchone()[0] == "kept.zip"
        assert raw.execute("SELECT value FROM cmx_settings WHERE key='filebox_pass'").fetchone()[0] == "kept-hash"
        tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "image_recognition" in tables
        assert "gemini_daily_usage" in tables


def test_gemini_daily_attempt_limit_is_atomic_and_rolls_by_day(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()

    assert db.claim_gemini_daily_attempt(2, day_utc="2026-08-01") is True
    assert db.claim_gemini_daily_attempt(2, day_utc="2026-08-01") is True
    assert db.claim_gemini_daily_attempt(2, day_utc="2026-08-01") is False
    assert db.gemini_daily_attempts(day_utc="2026-08-01") == 2
    assert db.claim_gemini_daily_attempt(2, day_utc="2026-08-02") is True
    assert db.gemini_daily_attempts(day_utc="2026-08-02") == 1
    assert db.claim_gemini_daily_attempt(0, day_utc="2026-08-03") is False


def test_image_recognition_shared_across_bots_by_sha256(tmp_path: Path):
    import sqlite3
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.record_local_ocr("abc123", text="hello", line_count=1, mean_confidence=0.9)
    # No bot_id column at all: every resident that looks up this hash sees the one
    # shared row, rather than each bot paying to recognise the same image again.
    for bot_id in ("gpt", "claude", "grok"):
        assert db.get_image_recognition("abc123")["local_ocr_text"] == "hello"
    with sqlite3.connect(db.path) as raw:
        count = raw.execute(
            "SELECT COUNT(*) FROM image_recognition WHERE image_sha256='abc123'"
        ).fetchone()[0]
    assert count == 1


def test_image_recognition_pending_state_roundtrips(tmp_path: Path):
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.record_local_ocr("h1", text="local text", line_count=3, mean_confidence=0.8)
    pending = db.list_pending_image_recognition()
    assert [row["image_sha256"] for row in pending] == ["h1"]
    row = db.get_image_recognition("h1")
    assert row["state"] == "pending"
    assert row["cloud_corrected_text"] is None
    assert row["cloud_description"] is None

    db.record_cloud_recognition("h1", corrected_text="fixed", description="a cat", keywords="cat,photo")
    row = db.get_image_recognition("h1")
    assert row["state"] == "done"
    assert row["cloud_corrected_text"] == "fixed"
    assert row["cloud_description"] == "a cat"
    assert row["search_keywords"] == "cat,photo"
    assert db.list_pending_image_recognition() == []


def test_image_recognition_rerecording_same_hash_does_not_duplicate(tmp_path: Path):
    import sqlite3
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    db.record_local_ocr("dup", text="first pass", line_count=1, mean_confidence=0.5)
    db.record_local_ocr("dup", text="second pass", line_count=2, mean_confidence=0.7)
    with sqlite3.connect(db.path) as raw:
        count = raw.execute(
            "SELECT COUNT(*) FROM image_recognition WHERE image_sha256='dup'"
        ).fetchone()[0]
    assert count == 1
    row = db.get_image_recognition("dup")
    assert row["local_ocr_text"] == "second pass"
    assert row["local_line_count"] == 2


def test_image_recognition_cloud_result_requires_existing_local_row(tmp_path: Path):
    import pytest
    db = Database(tmp_path / "cmx.sqlite3")
    db.initialize()
    with pytest.raises(RuntimeError, match="Unknown image_sha256"):
        db.record_cloud_recognition("missing", description="x")
