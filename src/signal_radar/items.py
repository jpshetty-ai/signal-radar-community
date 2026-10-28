"""The normalised news item every source adapter produces and the corpus stores.

Summaries and metadata only, never full article text (copyright, and the corpus stays diffable).
"""

from __future__ import annotations

import hashlib
import html as htmllib
import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field

_TRACKING_PARAMS = re.compile(r"^(utm_\w+|ref|ref_src|mc_cid|mc_eid|fbclid|gclid)$", re.I)


def canonical_url(url: str) -> str:
    """Lower-case scheme and host, drop tracking params and fragment. Used for dedupe, so keep it stable.

    HTML entities are decoded first: some pages double-escape ("&amp;amp;"), which would otherwise leave
    a parameter named "amp;utm_source" behind.
    """
    parts = urlsplit(htmllib.unescape(url.strip()))
    query = [(re.sub(r"^(amp;|;)+", "", k), v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    query = [(k, v) for k, v in query if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def item_id(url: str, title: str) -> str:
    """Stable id from canonical url + normalised title (the dedupe key from CLAUDE.md)."""
    norm_title = re.sub(r"\s+", " ", title).strip().lower()
    return hashlib.sha256(f"{canonical_url(url)}\n{norm_title}".encode()).hexdigest()[:16]


class Item(BaseModel):
    id: str
    source_id: str
    date: date                       # the day the source published it (newsletter issue date)
    url: str                         # canonical, tracking stripped
    title: str
    summary: str = ""
    section: str | None = None       # e.g. "Headlines & Launches"
    tags: list[str] = Field(default_factory=list)
    read_minutes: int | None = None
    content_kind: str | None = None  # e.g. "GitHub Repo", "Website"; None for plain articles
    edition: str | None = None       # e.g. "TLDR AI"
    engagement: dict[str, int] = Field(default_factory=dict)  # as reported by the source, e.g. {"likes": 16862}
    origin_ref: str | None = None    # message id / feed entry id, for tracing back
    fetched_at: datetime | None = None
