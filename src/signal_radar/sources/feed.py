"""RSS/Atom adapter: one feed URL = one source. Read-only, no state beyond the ingest cursor.

Feeds only carry their newest entries (Sifted ~4 days, Tech.eu ~3), so whatever the cursor says, an entry that
rolled off the feed before the next run is gone. Run ingest at least daily for busy feeds.
"""

from __future__ import annotations

import calendar
import html as htmllib
import re
from collections.abc import Callable
from datetime import UTC, datetime

import feedparser
import httpx
from bs4 import BeautifulSoup

from ..config import FeedSource
from ..items import Item, canonical_url, item_id
from .base import AdapterBase, FetchReport

USER_AGENT = "signal-radar/0.2 (personal research)"
HEAD_LIMIT = 512 * 1024  # stop reading an article page after this many bytes even if </head> never came
_TRUNCATED = re.compile(r"(\.\.\.|…|\[…\]|\[\.\.\.\])\s*$")

FeedFetch = Callable[[str], bytes]
HeadFetch = Callable[[str], str | None]


class FeedError(RuntimeError):
    """The feed could not be fetched or is not RSS/Atom."""


def default_feed_fetch(url: str) -> bytes:
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise FeedError(f"could not fetch feed {url}: {e}") from e
    return r.content


def default_head_fetch(url: str) -> str | None:
    """Read an article page only up to </head>: Sifted pages are ~3 MB, the meta tags sit in the first few KB."""
    buf = b""
    try:
        with httpx.stream("GET", url, timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as r:
            if r.status_code != 200:
                return None
            for chunk in r.iter_bytes():
                buf += chunk
                if b"</head>" in buf or len(buf) > HEAD_LIMIT:
                    break
    except httpx.HTTPError:
        return None
    return buf.decode("utf-8", errors="replace")


def page_description(head_html: str) -> str:
    soup = BeautifulSoup(head_html, "html.parser")
    for sel in ('meta[property="og:description"]', 'meta[name="description"]', 'meta[name="twitter:description"]'):
        tag = soup.select_one(sel)
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    return ""


def clean_summary(raw: str, title: str, strip: list[str], max_chars: int) -> str:
    """HTML to one paragraph of text: boilerplate patterns removed, a repeated title dropped, cut at a word."""
    text = BeautifulSoup(raw, "html.parser").get_text(" ") if "<" in raw else raw
    text = re.sub(r"\s+", " ", htmllib.unescape(text)).strip()
    text = re.sub(r" ([,.;:!?])", r"\1", text)  # get_text(" ") puts a space where an inline tag ended
    for pattern in strip:
        text = re.sub(pattern, " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if title and text.lower().startswith(title.strip().lower()):
        text = text[len(title.strip()):].lstrip(" :.-")
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return text


def _entry_time(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime.fromtimestamp(calendar.timegm(parsed), UTC) if parsed else None


class FeedAdapter(AdapterBase):
    def __init__(self, source: FeedSource, fetch_feed: FeedFetch = default_feed_fetch,
                 fetch_head: HeadFetch = default_head_fetch):
        self.source = source
        self.source_id = source.id
        self.fetch_feed = fetch_feed
        self.fetch_head = fetch_head

    def fetch_report(self, since: datetime) -> FetchReport:
        report = FetchReport(source_id=self.source_id, since=since)
        feed = feedparser.parse(self.fetch_feed(str(self.source.url)))
        if feed.bozo and not feed.entries:
            raise FeedError(f"{self.source.url} is not a parseable RSS/Atom feed: {feed.bozo_exception}")
        edition = self.source.edition or feed.feed.get("title") or self.source_id
        fetched_at = datetime.now(UTC)
        seen: set[str] = set()
        undated = 0
        oldest: datetime | None = None
        for entry in feed.entries:
            report.scanned += 1
            title = re.sub(r"\s+", " ", htmllib.unescape(entry.get("title", ""))).strip()
            link = entry.get("link")
            if not title or not link:
                report.warnings.append(f"entry without title or link skipped: {entry.get('id', '?')}")
                continue
            when = _entry_time(entry)
            if when is None:
                undated += 1
                when = fetched_at
            oldest = when if oldest is None or when < oldest else oldest
            if when < since:
                continue
            report.matched += 1
            iid = item_id(link, title)
            if iid in seen:
                continue
            seen.add(iid)
            report.items.append(Item(
                id=iid, source_id=self.source_id, date=when.date(), url=canonical_url(link), title=title,
                summary=self._summary(entry, title, link), section=self._section(entry), tags=self.source.tags,
                edition=edition, origin_ref=entry.get("id") or link, fetched_at=fetched_at,
            ))
        if undated:
            report.warnings.append(f"{undated} entr(y/ies) had no date; dated today")
        if oldest and oldest > since and report.scanned:
            # The feed starts after `since`: anything older already rolled off and cannot be recovered here.
            report.warnings.append(f"feed only reaches back to {oldest:%Y-%m-%d}; earlier entries are not in it")
        return report

    def _summary(self, entry, title: str, link: str) -> str:
        """Feed description first, then a short excerpt of the entry content, then (page_meta) the article's meta
        description. Never the full article text (copyright, see items.py)."""
        args = (title, self.source.strip, self.source.max_summary_chars)
        description = clean_summary(entry.get("summary", ""), title, self.source.strip, 100_000)
        if _TRUNCATED.search(description) and entry.get("content"):
            description = ""  # a CMS auto-excerpt cut mid-sentence (WordPress "..."); the content's lead reads better
        text = clean_summary(description, *args)
        if not text and entry.get("content"):
            text = clean_summary(entry["content"][0].get("value", ""), *args)
        if not text and self.source.page_meta:  # last: it costs one HTTP request per entry
            head = self.fetch_head(link)
            text = clean_summary(page_description(head), *args) if head else ""
        return text

    @staticmethod
    def _section(entry) -> str | None:
        terms = [t.get("term", "").strip() for t in entry.get("tags", [])]
        terms = [t for t in terms if t and t.lower() not in {"general", "uncategorized", "uncategorised"}]
        return terms[0] if terms else None
