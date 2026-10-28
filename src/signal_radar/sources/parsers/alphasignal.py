"""Parser for AlphaSignal (alphasignal.ai) daily issues.

Structure, checked against 12 real issues (2026-09-20 .. 2026-10-02):
- a "Summary" block (table of contents, skipped), then feature stories: a section label cell ("Top News",
  "Top Repo", "Top Paper"), a td.h1 title, an engagement cell ("16,862 Likes", "3,388 Downloads"), body <p>s
  and a READ MORE link
- a td.h1 "Signals" list: each entry is a link followed by its engagement cell
- sponsored blocks: a "Presented by X" cell and *no* engagement count; three per weekday issue, one of them
  sometimes inside the Signals list (linking through srv.buysellads.com)
- "Sunday Deep Dive" issues are single essays without engagement data: skipped (SkipMessage)

Links: every link in the email is an opaque per-subscriber redirect (app.alphasignal.ai/c?uid=<you>&lid=...).
Resolving one would register a click under your identity, so we never request them. AlphaSignal's terms
(alphasignal.ai/terms, §6) also forbid automated access to its site, so we no longer fetch the public web version
either (ADR-0015). Everything comes from the email itself: titles, sections, engagement and summaries. Each item
links to the issue's public page (alphasignal.ai/email/<issue id>, the link the email offers for forwarding),
built from the id and never requested. Items stored before 2026-10-05 keep the article URLs resolved then.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from ...items import Item, item_id
from .common import SkipMessage

WEB_VERSION = "https://alphasignal.ai/email/{issue}"
_ISSUE_ID = re.compile(r"alphasignal\.ai(?:/|%2F)email(?:/|%2F)([0-9a-f]{16})", re.I)
_METRIC = re.compile(r"^([\d,.]+)\s*([KkMm]?)\s+(Likes|Downloads|Stars|Views|Upvotes|Reads)$")
_SECTION_LABEL = re.compile(r"^Top [A-Z][a-z]+$")
_SUMMARY_CAP = 600


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("‌", " ")).strip()


def _metric(text: str) -> tuple[str, int] | None:
    m = _METRIC.match(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    value *= {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return m.group(3).lower(), int(value)


def _cap(text: str) -> str:
    if len(text) <= _SUMMARY_CAP:
        return text
    cut = text[:_SUMMARY_CAP]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > 200 else cut.rstrip() + "…"


@dataclass
class _Entry:
    title: str
    section: str
    sponsored: bool = False
    url: str | None = None
    engagement: dict[str, int] = field(default_factory=dict)
    paras: list[str] = field(default_factory=list)


def _events(soup: BeautifulSoup):
    """Flatten the table soup into an ordered stream of meaningful cells, links and paragraphs."""
    for el in soup.find_all(["td", "a", "p"]):
        if el.name == "a":
            if el.get("href"):
                yield "link", _clean(el.get_text(" ")), el["href"]
            continue
        if el.name == "p":
            yield "p", _clean(el.get_text(" ")), None
            continue
        if el.find("td"):
            continue  # only leaf cells carry content
        text = _clean(el.get_text(" "))
        if not text:
            continue
        if "h1" in (el.get("class") or []):
            yield "h1", text, None
        elif _metric(text):
            yield "metric", text, None
        elif text.startswith("Presented by"):
            yield "presented", text, None
        elif _SECTION_LABEL.match(text) or text == "Sunday Deep Dive":
            yield "label", text, None


def parse_issue_doc(doc_html: str) -> list[_Entry]:
    """Entries (stories + signals, sponsors flagged) from one issue's HTML, email or web version alike."""
    soup = BeautifulSoup(doc_html, "html.parser")
    entries: list[_Entry] = []
    mode = "intro"   # intro -> summary -> story | signals
    label = "Top News"
    pending_sponsor = False
    current: _Entry | None = None

    for kind, text, href in _events(soup):
        if kind == "label":
            if text == "Sunday Deep Dive":
                raise SkipMessage("Sunday Deep Dive: single-topic essay without engagement data")
            label = text
        elif kind == "h1":
            if text == "Summary":
                mode = "summary"
            elif text == "Signals":
                mode, current = "signals", None
            else:
                mode = "story"
                current = _Entry(title=text, section=label, sponsored=pending_sponsor)
                entries.append(current)
                pending_sponsor = False
        elif kind == "presented":
            if mode == "signals" and current is not None and not current.engagement:
                current.sponsored = True   # Signals list: "Presented by" sits where the count would be
            else:
                pending_sponsor = True      # feature blocks: "Presented by" precedes the sponsored h1
        elif kind == "metric" and current is not None:
            name, value = _metric(text)  # type: ignore[misc]
            current.engagement[name] = value
        elif kind == "p" and mode == "story" and current is not None and text:
            current.paras.append(text)
        elif kind == "link" and href:
            if mode == "story" and current is not None:
                if text.upper() == "READ MORE" or current.url is None:
                    current.url = href
            elif mode == "signals" and text:
                current = _Entry(title=re.sub(r"^\d+\.\s*", "", text), section="Signals")
                current.url = href
                entries.append(current)
    return entries


def issue_url(issue: str) -> str:
    """The issue's public page: what an item links to. Built, never fetched (terms §6, ADR-0015)."""
    return WEB_VERSION.format(issue=issue)


def parse_alphasignal(
    html: str,
    *,
    source_id: str,
    received_at: datetime,
    origin_ref: str | None = None,
    tags: list[str] | None = None,
) -> list[Item]:
    entries = parse_issue_doc(html)  # a Sunday deep dive raises SkipMessage here
    issue = _ISSUE_ID.search(html)
    if not issue:
        # Permanent, not transient: retrying can't help, so skip rather than defer (a deferral holds the cursor).
        raise SkipMessage("no web-version link in the email (special edition?), so items would have nothing to link to")
    url = issue_url(issue.group(1))

    items: list[Item] = []
    seen: set[str] = set()
    for e in entries:
        if e.sponsored or not e.engagement:
            continue  # sponsors carry no engagement count; that is the most stable marker
        iid = item_id(url, e.title)  # one issue URL, so the title tells the stories apart
        if iid in seen:
            continue
        seen.add(iid)
        items.append(Item(
            id=iid, source_id=source_id, date=received_at.date(), url=url, title=e.title,
            summary=_cap(" ".join(e.paras)), section=e.section, tags=list(tags or []),
            engagement=e.engagement, edition="AlphaSignal", origin_ref=origin_ref,
        ))
    return items
