from cmx_mcp.compact import extract_links, strip_html, timeline_preview


def _autolink(url: str, shown: str) -> str:
    """Reproduce Mastodon's autolink markup: the visible slice plus hidden ends."""
    hidden_head, rest = url.split(shown, 1)
    return (
        f'<a href="{url}" rel="nofollow noopener" target="_blank">'
        f'<span class="invisible">{hidden_head}</span>'
        f'<span class="ellipsis">{shown}</span>'
        f'<span class="invisible">{rest}</span></a>'
    )


def test_strip_html_preserves_paragraphs():
    assert strip_html("<p>one<br>two</p><p>three</p>") == "one\ntwo\nthree"


def test_xhs_share_becomes_note_plus_placeholder():
    url = "http://xhslink.cn/o/6ZScCdILPdc"
    html = (
        "<p>蜂蜜柠檬脆皮鸡翅！！ "
        + _autolink(url, "xhslink.cn/o/6ZScCdILPdc")
        + " 把这段复制好，然后去【小红书】就能看笔记。</p>"
    )
    assert strip_html(html) == "蜂蜜柠檬脆皮鸡翅！！ 【url-xhs】"
    assert extract_links(html) == [url]


def test_unknown_host_gets_a_bare_placeholder_not_its_domain():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    html = f"<p>see {_autolink(url, 'youtube.com/watch?v=dQw4w9WgXcQ')}</p>"
    assert strip_html(html) == "see 【url】"
    assert extract_links(html) == [url]


def test_boilerplate_matching_never_eats_ordinary_writing():
    html = "<p>今天在小红书上看到一个菜谱，复制下来了，明天试试</p>"
    assert strip_html(html) == "今天在小红书上看到一个菜谱，复制下来了，明天试试"


def test_share_blurb_is_cut_at_the_link_and_later_lines_survive():
    """Real posts, and the variants that defeated template matching."""
    url = "http://xhslink.cn/o/oCbVPLX43G"
    shown = "xhslink.cn/o/oCbVPLX43G"
    cases = [
        (
            f"<p>呼声很高的钥匙扣来啦！ {_autolink(url, shown)} 先复制再打开【小红书】，笔记内容马上呈现。<br>喜欢的<br>发图作为测试</p>",
            "呼声很高的钥匙扣来啦！ 【url-xhs】\n喜欢的\n发图作为测试",
        ),
        (
            f"<p>跳得很好，下次不许跳了😑 {_autolink(url, shown)} 来【小红书】发现这篇笔记的精彩~<br>猫猫</p>",
            "跳得很好，下次不许跳了😑 【url-xhs】\n猫猫",
        ),
        (
            f"<p>蜂蜜柠檬脆皮鸡翅！！ {_autolink(url, shown)} 把这段复制好，然后去【小红书】就能看笔记。</p>",
            "蜂蜜柠檬脆皮鸡翅！！ 【url-xhs】",
        ),
    ]
    for html, expected in cases:
        assert strip_html(html) == expected


def test_cutting_is_confined_to_share_platforms_and_to_one_line():
    url = "https://example.com/article"
    html = f"<p>看这个 {_autolink(url, 'example.com/article')} 我的评论应该留着</p>"
    assert strip_html(html) == "看这个 【url】 我的评论应该留着"


def test_mentions_and_hashtags_keep_their_own_text():
    html = (
        '<p><a href="https://pi.invalid/@alice" class="u-url mention">@<span>alice</span></a>'
        ' 聊聊 <a href="https://pi.invalid/tags/cooking" rel="tag">#<span>cooking</span></a></p>'
    )
    assert strip_html(html) == "@alice 聊聊 #cooking"


def test_timeline_preview_is_sparse_normalized_and_bounded():
    raw = {"id": "wrapper", "reblog": {"id": "source", "content": "<p>Hello\n   world " + "界" * 80 + "</p>",
           "account": {"id": "secret", "acct": "alice", "display_name": "Alice"},
           "replies_count": 4, "media_attachments": [{"url": "secret"}, {"url": "secret2"}],
           "created_at": "secret", "visibility": "private"}}
    result = timeline_preview(raw, 50)
    assert set(result) == {"id", "author", "preview", "replies", "media"}
    assert result["id"] == "source"
    assert result["preview"].startswith("Hello world")
    assert len(result["preview"]) <= 50
    assert result["replies"] == 4 and result["media"] == 2


def test_feed_advertises_recognised_text_by_size_without_spending_it():
    from cmx_mcp.compact import compact_v2_status

    raw = {
        "id": "1", "content": "<p>看这个</p>", "account": {"acct": "a"},
        "media_attachments": [{"id": "m1", "type": "image", "description": "菜单"}],
    }
    recognitions = {"m1": {"local_ocr_text": "蜂蜜柠檬脆皮鸡翅 鸡翅中 500g 柠檬 1个"}}
    media = compact_v2_status(raw, recognitions)["media"][0]
    assert media == {"type": "image", "alt": "菜单", "ocr_chars": 23}
    assert "蜂蜜" not in str(media)
    # No recognition yet, or an image with no text at all, adds nothing.
    assert compact_v2_status(raw)["media"][0] == {"type": "image", "alt": "菜单"}


def test_expanding_media_separates_what_was_read_from_what_was_guessed():
    from cmx_mcp.compact import compact_media

    row = {
        "local_ocr_text": "蜂密柠檬脆皮鸡翅",
        "cloud_corrected_text": "蜂蜜柠檬脆皮鸡翅",
        "cloud_description": "一张手写餐牌的照片",
        "uncertain_text": "第三行价格看不清",
    }
    result = compact_media({"id": "m1", "type": "image", "description": "菜单"}, row)
    assert result["ocr"] == "蜂蜜柠檬脆皮鸡翅"
    assert result["description"] == "一张手写餐牌的照片"
    assert result["uncertain"] == "第三行价格看不清"
    assert result["ocr"] != row["local_ocr_text"]


def test_timeline_preview_omits_zero_replies_and_media():
    assert timeline_preview({"id": "1", "content": "ok", "account": {"acct": "a"}}) == {
        "id": "1", "author": "a", "preview": "ok"
    }
