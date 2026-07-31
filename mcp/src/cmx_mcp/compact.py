from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit


# Hosts worth naming in the placeholder. Anything absent stays a bare 【url】 —
# the point is to spend a handful of characters, not to smuggle the address back
# in as a domain. Adding an entry is one line.
LINK_ALIASES = {
    "xhslink.cn": "xhs",
    "xhslink.com": "xhs",
    "xiaohongshu.com": "xhs",
}

# Share blurbs are matched as whole known templates, never by keyword. "复制" and
# "小红书" both appear in ordinary writing ("今天在小红书上看到一个菜谱，复制下来了"),
# so keyword matching would eat the resident's own words. Missing a template is
# recoverable; deleting someone's sentence is not.
_SHARE_BOILERPLATE = re.compile(
    r"[，,]?\s*(?:复制本条信息|把这段复制好|复制这段内容|复制打开)[^。！!？?\n]*[。！!]?"
)


def link_placeholder(url: str) -> str:
    """Return the token that stands in for `url` in resident-facing text."""
    host = urlsplit(url).hostname or ""
    host = host[4:] if host.startswith("www.") else host
    for domain, alias in LINK_ALIASES.items():
        if host == domain or host.endswith("." + domain):
            return f"【url-{alias}】"
    return "【url】"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"p", "br", "li", "blockquote"}:
            self.parts.append("\n")
        elif tag == "a":
            self._anchor_href = dict(attrs).get("href") or None
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "li", "blockquote"}:
            self.parts.append("\n")
        elif tag == "a":
            shown = "".join(self._anchor_parts)
            href = self._anchor_href
            compact = "".join(shown.split())
            # Mastodon autolinks a bare URL by slicing it across visible and
            # `invisible` spans, so the rendered text is a substring of the href.
            # Mentions and hashtags must be excluded by their leading sigil, not
            # by that substring test: `@alice` does occur inside the mention's own
            # href (https://host/@alice) and would otherwise be rewritten to it.
            if href and compact and not compact.startswith(("@", "#")) and compact in href:
                self.links.append(href)
                self.parts.append(link_placeholder(href))
            else:
                self.parts.append(shown)
            self._anchor_href = None
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_parts.append(data)
        else:
            self.parts.append(data)

    def text(self) -> str:
        joined = _SHARE_BOILERPLATE.sub("", "".join(self.parts))
        lines = [" ".join(line.split()) for line in joined.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def extract_links(value: str | None) -> list[str]:
    """Return the full hrefs the placeholders stand for, in the order they appear."""
    parser = _TextExtractor()
    parser.feed(value or "")
    return parser.links


def strip_html(value: str | None) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    return parser.text()


def timeline_preview(raw: dict[str, Any], max_chars: int = 50) -> dict[str, Any]:
    """Return the deliberately tiny representation used by remote timeline browsing."""
    source = raw.get("reblog") or raw
    account = source.get("account") or {}
    text = re.sub(r"\s+", " ", strip_html(source.get("content"))).strip()
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 1)].rstrip() + "…"
    result: dict[str, Any] = {
        "id": str(source.get("id") or raw.get("id") or ""),
        "author": str(account.get("acct") or account.get("username") or ""),
        "preview": text,
    }
    replies = int(source.get("replies_count") or 0)
    if replies:
        result["replies"] = replies
    media_count = len(source.get("media_attachments") or [])
    if media_count:
        result["media"] = media_count
    return result


def compact_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(account.get("id") or ""),
        "acct": account.get("acct") or account.get("username") or "",
        "display_name": account.get("display_name") or "",
        "bot": bool(account.get("bot", False)),
        "locked": bool(account.get("locked", False)),
    }


def compact_media(media: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(media.get("id") or ""),
        "type": media.get("type"),
        "description": media.get("description"),
        "url": media.get("url"),
    }


def compact_status(raw: dict[str, Any]) -> dict[str, Any]:
    wrapper = raw
    source = raw.get("reblog") or raw
    account = source.get("account") or {}
    wrapper_account = wrapper.get("account") or {}
    mentions = [
        {"id": str(item.get("id") or ""), "acct": item.get("acct") or ""}
        for item in source.get("mentions") or []
    ]
    return {
        "id": str(wrapper.get("id") or ""),
        "interaction_target_id": str(source.get("id") or ""),
        "author": compact_account(account),
        "boosted_by": compact_account(wrapper_account) if raw.get("reblog") else None,
        "text": strip_html(source.get("content")),
        "spoiler_text": source.get("spoiler_text") or "",
        "sensitive": bool(source.get("sensitive", False)),
        "created_at": source.get("created_at"),
        "edited_at": source.get("edited_at"),
        "visibility": source.get("visibility"),
        "reply_to_id": source.get("in_reply_to_id"),
        "mentions": mentions,
        "media": [compact_media(item) for item in source.get("media_attachments") or []],
        "favourited": bool(source.get("favourited", False)),
        "bookmarked": bool(source.get("bookmarked", False)),
        "reblogged": bool(source.get("reblogged", False)),
    }


def compact_v2_status(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the sparse Phase A representation; omit absent/empty fields."""
    wrapper = raw
    source = raw.get("reblog") or raw
    account = source.get("account") or {}
    result: dict[str, Any] = {
        "id": str(source.get("id") or wrapper.get("id") or ""),
        "author": str(account.get("acct") or account.get("username") or ""),
        "at": source.get("created_at"),
        "text": strip_html(source.get("content")),
    }
    optional = {
        "reply_to": source.get("in_reply_to_id"),
        "via": (raw.get("account") or {}).get("acct") if raw.get("reblog") else None,
        "vis": source.get("visibility") if source.get("visibility") not in (None, "private") else None,
        "cw": source.get("spoiler_text") or None,
    }
    if source.get("visibility") == "direct":
        recipients = [str(item.get("acct") or "") for item in source.get("mentions") or []]
        recipients = [item for item in recipients if item]
        if recipients:
            optional["to"] = recipients
    for key, value in optional.items():
        if value not in (None, "", [], False):
            result[key] = value
    attachments = source.get("media_attachments") or []
    if attachments:
        result["media"] = [
            {key: value for key, value in {
                "type": item.get("type"),
                "alt": item.get("description"),
            }.items() if value not in (None, "")}
            for item in attachments
        ]
    poll = source.get("poll")
    if poll:
        result["poll"] = {
            key: value for key, value in {
                "options": [str(item.get("title") or "") for item in poll.get("options") or []],
                "expired": poll.get("expired"),
                "multiple": poll.get("multiple"),
                "voted": poll.get("voted"),
            }.items() if value not in (None, "", [], False)
        }
    state = {
        key: value for key, value in {
            "favourite": source.get("favourited"),
            "bookmark": source.get("bookmarked"),
            "reblog": source.get("reblogged"),
        }.items() if value
    }
    if state:
        result["state"] = state
    return {key: value for key, value in result.items() if value not in (None, "", [], False)}
