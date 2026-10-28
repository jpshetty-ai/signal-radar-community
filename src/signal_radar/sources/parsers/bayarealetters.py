"""Parser for Bay Area Letters (bayarealetters.com) daily issues (ADR-0016).

Structure, checked against real issues from 2026-07-06 to 2026-10-09 (Ghost newsletter template):
- each story is an <h3> title followed by a paragraph starting "TL;DR:" (two sentences) and "Read more →"
- other <h3>s ("Let the Future Come to Your Inbox", "Keep reading") and the <h4> previews of older issues have no
  TL;DR paragraph, so "h3 + TL;DR" is the story rule
- sponsor blocks would sit in Ghost's call-to-action card (.kg-cta-card); none seen yet, skipped regardless

Links: every link is a per-subscriber redirect (bayarealetters.com/r/<id>?m=<subscriber uuid>), including "View
in browser". Requesting one would register a click under your identity, so none is followed. The site publishes
no terms of use (checked 2026-10-09): private reading only, never in the public edition. Items link to the issue
in Gmail (search by Message-ID), which works for the mailbox owner only.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from ...items import Item, item_id
from .common import SkipMessage

_TLDR = re.compile(r"^\s*TL;DR:\s*", re.I)
_READ_MORE = re.compile(r"\s*Read more\s*→?\s*$", re.I)
_SUMMARY_CAP = 600


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("‌", " ").replace("͏", " ")).strip()


def issue_url(origin_ref: str | None) -> str:
    """Where an item links: the issue in the owner's Gmail. Never a publisher URL (all are tracked redirects)."""
    if not origin_ref:
        return "https://mail.google.com/mail/"
    return "https://mail.google.com/mail/#search/" + quote(f"rfc822msgid:{origin_ref.strip('<>')}", safe="")


def parse_stories(html: str) -> list[tuple[str, str]]:
    """(title, summary) for every story in one issue."""
    soup = BeautifulSoup(html, "html.parser")
    stories: list[tuple[str, str]] = []
    title: str | None = None  # the last <h3> still waiting for its TL;DR, in document order
    for el in soup.find_all(["h3", "h4", "p"]):
        if el.find_parent(class_="kg-cta-card"):
            continue
        if el.name in ("h3", "h4"):
            title = _clean(el.get_text(" ")) if el.name == "h3" else None
            continue
        text = _clean(el.get_text(" "))
        if title and _TLDR.match(text):
            summary = _READ_MORE.sub("", _TLDR.sub("", text))
            if summary:
                stories.append((title, summary[:_SUMMARY_CAP]))
            title = None
    return stories


def parse_bayarealetters(
    html: str,
    *,
    source_id: str,
    received_at: datetime,
    origin_ref: str | None = None,
    tags: list[str] | None = None,
) -> list[Item]:
    stories = parse_stories(html)
    if not stories:
        raise SkipMessage("no TL;DR stories (not a daily issue, or the template changed)")
    url = issue_url(origin_ref)
    items, seen = [], set()
    for title, summary in stories:
        iid = item_id(url, title)  # one issue link, so the title tells the stories apart
        if iid in seen:
            continue
        seen.add(iid)
        items.append(Item(id=iid, source_id=source_id, date=received_at.date(), url=url, title=title,
                          summary=summary, tags=list(tags or []), edition="Bay Area Letters",
                          origin_ref=origin_ref))
    return items
