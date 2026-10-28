"""Parser for TL;DR's public web archive pages (tldr.tech/<edition>/<YYYY-MM-DD>), used for backfill.

Structure (checked 2026-10-03): section titles are <h3> outside any <article>; each item is
<article><a href=REAL_URL><h3>Title (N minute read)</h3></a><div class="newsletter-html">summary</div></article>.
Unlike the email, links are direct (no click tracker) and TL;DR's short links are already resolved.
Titles and URLs normalise exactly as in the email parser, so the two produce the same item ids.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from ...items import Item, canonical_url, item_id
from .tldr import _READ_TIME, _TRAILING_PAREN, _clean, _is_own_link


def parse_tldr_web(
    html: str,
    *,
    source_id: str,
    issue_date: date,
    edition: str | None = None,
    tags: list[str] | None = None,
    origin_ref: str | None = None,
    fetched_at: datetime | None = None,
) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    section: str | None = None
    items: list[Item] = []
    seen: set[str] = set()
    for el in soup.find_all(["h3", "article"]):
        if el.name == "h3":
            if el.find_parent("article") is None:
                text = _clean(el.get_text(" "))
                if text:
                    section = text
            continue
        link, heading = el.find("a", href=True), el.find("h3")
        if link is None or heading is None:
            continue
        raw_title = _clean(heading.get_text(" "))
        title, read_minutes, kind = raw_title, None, None
        m = _TRAILING_PAREN.match(raw_title)
        if m:
            title, marker = m.group(1), m.group(2)
            if marker.lower() == "sponsor":
                continue
            rt = _READ_TIME.search(marker)
            if rt:
                read_minutes = int(rt.group(1))
            else:
                kind = marker
        if _is_own_link(link["href"]):
            continue
        url = canonical_url(link["href"])
        body = el.find("div", class_="newsletter-html")
        summary = _clean(body.get_text(" ")) if body else ""
        iid = item_id(url, title)
        if iid in seen:
            continue
        seen.add(iid)
        items.append(Item(
            id=iid, source_id=source_id, date=issue_date, url=url, title=title, summary=summary, section=section,
            tags=list(tags or []), read_minutes=read_minutes, content_kind=kind, edition=edition,
            origin_ref=origin_ref, fetched_at=fetched_at,
        ))
    return items


def edition_from_page(html: str) -> str | None:
    """'TLDR AI' style edition name from the page, when present."""
    m = re.search(r"\bTLDR(?: [A-Z][A-Za-z]+)?(?= \d{4}-\d{2}-\d{2})", BeautifulSoup(html, "html.parser").get_text(" "))
    return m.group(0) if m else None
