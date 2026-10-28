"""Parser for TL;DR newsletters (tldr.tech): one email -> many Items.

Structure, checked against real issues (2026-10):
- an <h1> "TLDR AI 2026-10-02" names the edition and issue date
- section <h1>s ("Headlines & Launches", "Quick Links", ...) precede their items
- an item is a div.text-block that starts with <a><strong>Title (N minute read)</strong></a>, possibly
  inside <span> wrappers (the raw MIME body has them; some viewers strip them),
  followed by a 1-4 sentence summary
- every link is wrapped: https://tracking.tldrnewsletter.com/CL0/<url-encoded target>/1/<msg>/<sig>
- sponsored items carry "(Sponsor)" in the title; TL;DR's own job ads link to its own domains

Unwrapping matters twice: dedupe keys on the real URL, and following the wrapped link would register a click.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ...items import Item, canonical_url, item_id

_EDITION = re.compile(r"^(TLDR(?:\s+.+?)?)\s+(\d{4}-\d{2}-\d{2})$")
_TRAILING_PAREN = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_READ_TIME = re.compile(r"(\d+)\s+minute read", re.I)
_TRACKER_HOST = "tracking.tldrnewsletter.com"
# Links to TL;DR itself (job ads, advertise, manage subscription) are not news.
_OWN_HOSTS = ("tldr.tech", "a.tldrnewsletter.com", "jobs.ashbyhq.com")


def unwrap_tracking(url: str) -> str:
    parts = urlsplit(url)
    if parts.netloc.lower() == _TRACKER_HOST and parts.path.startswith("/CL0/"):
        return unquote(parts.path[len("/CL0/"):].split("/", 1)[0])
    return url


def _is_own_link(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in _OWN_HOSTS)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("‌", " ")).strip()


_WRAPPERS = {"span", "font", "b"}


def _leading_link(block: Tag) -> Tag | None:
    """The <a> wrapping a <strong> title if it is the block's first content, else None.

    Raw issues wrap it in inline elements (div > span > a > span > strong); step through those wrappers.
    """
    node: Tag = block
    while True:
        first = next((c for c in node.children if not (isinstance(c, NavigableString) and not c.strip())), None)
        if not isinstance(first, Tag):
            return None
        if first.name == "a":
            return first if first.find("strong") else None
        if first.name not in _WRAPPERS:
            return None
        node = first


def parse_tldr(
    html: str,
    *,
    source_id: str,
    received_at: datetime,
    origin_ref: str | None = None,
    tags: list[str] | None = None,
) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    edition: str | None = None
    issue_date: date = received_at.date()
    section: str | None = None
    items: list[Item] = []
    seen: set[str] = set()

    for el in soup.find_all(["h1", "div"]):
        if el.name == "h1":
            text = _clean(el.get_text(" "))
            m = _EDITION.match(text)
            if m and edition is None:
                edition, issue_date = m.group(1), date.fromisoformat(m.group(2))
            elif text:
                section = text
            continue
        if "text-block" not in (el.get("class") or []):
            continue
        link = _leading_link(el)
        if link is None or not link.get("href"):
            continue

        raw_title = _clean(link.get_text(" "))
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

        url = unwrap_tracking(link["href"])
        if _is_own_link(url):
            continue
        url = canonical_url(url)

        # Everything in the block after the title link, however deeply the template nests it.
        full = _clean(el.get_text(" "))
        summary = full[len(raw_title):].strip() if full.startswith(raw_title) else full
        iid = item_id(url, title)
        if iid in seen:
            continue
        seen.add(iid)
        items.append(Item(
            id=iid, source_id=source_id, date=issue_date, url=url, title=title, summary=summary,
            section=section, tags=list(tags or []), read_minutes=read_minutes, content_kind=kind,
            edition=edition, origin_ref=origin_ref,
        ))
    return items
