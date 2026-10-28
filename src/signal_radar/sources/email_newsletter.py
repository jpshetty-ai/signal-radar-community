"""Email newsletter adapter: a mailbox folder + match rule + parser = a source.

The adapter only reads. It never marks messages read, moves or deletes them, so it can run next to a human
reading the same newsletters in Outlook.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Protocol

from ..config import EmailNewsletterSource
from .base import AdapterBase, FetchReport
from .m365 import MailMessage
from .parsers import ParseDeferred, SkipMessage, get_parser


class MailReader(Protocol):
    def list_messages(self, folder: str, since: datetime, with_body: bool = True,
                      senders: list[str] | None = None) -> Iterator[MailMessage]: ...


class EmailNewsletterAdapter(AdapterBase):
    def __init__(self, source: EmailNewsletterSource, mail: MailReader):
        self.source = source
        self.source_id = source.id
        self.mail = mail
        self.parse = get_parser(source.parser)

    def fetch_report(self, since: datetime) -> FetchReport:
        report = FetchReport(source_id=self.source_id, since=since)
        fetched_at = datetime.now(UTC)
        seen: set[str] = set()
        for msg in self.mail.list_messages(self.source.folder, since, senders=self.source.match.from_ or None):
            report.scanned += 1
            if not self.source.match.matches(msg.from_address, msg.from_name, msg.subject):
                continue
            report.matched += 1
            try:
                items = self.parse(
                    msg.html or "",
                    source_id=self.source_id,
                    received_at=msg.received_at,
                    origin_ref=msg.internet_message_id or msg.id,
                    tags=self.source.tags,
                )
            except SkipMessage as e:
                report.skipped.append(f"{msg.received_at:%Y-%m-%d} {msg.subject!r}: {e}")
                continue
            except ParseDeferred as e:
                report.deferred += 1
                report.deferred_dates.append(msg.received_at)
                report.warnings.append(f"deferred {msg.subject!r} ({msg.received_at:%Y-%m-%d}): {e}")
                continue
            if not items:
                # A matched issue with zero items almost always means the publisher changed their template.
                report.warnings.append(f"parser {self.source.parser!r} found no items in {msg.subject!r} ({msg.received_at:%Y-%m-%d})")
            for item in items:
                if item.id in seen:
                    continue
                seen.add(item.id)
                report.items.append(item.model_copy(update={"fetched_at": fetched_at}))
        if report.scanned and not report.matched:
            report.warnings.append(
                f"{report.scanned} message(s) in {self.source.folder!r} but none matched; "
                "check the match rule (suggest_newsletter_filter shows sender names)"
            )
        return report


def sender_groups(messages: Iterable[MailMessage], samples: int = 3) -> list[dict]:
    """Group messages by (address, display name) and propose a match block for each group."""
    groups: dict[tuple[str, str], dict] = {}
    for m in messages:
        key = (m.from_address.lower(), m.from_name)
        g = groups.setdefault(key, {
            "from_address": m.from_address, "from_name": m.from_name, "count": 0, "sample_subjects": [],
            "latest": m.received_at.isoformat(),
        })
        g["count"] += 1
        if len(g["sample_subjects"]) < samples:
            g["sample_subjects"].append(m.subject)
    shared_address = {}
    for addr, _ in groups:
        shared_address[addr] = shared_address.get(addr, 0) + 1
    out = []
    for (addr, name), g in sorted(groups.items(), key=lambda kv: -kv[1]["count"]):
        domain = "@" + addr.split("@", 1)[1] if "@" in addr else addr
        match: dict = {"from": [domain]}
        if shared_address[addr] > 1 and name:
            match["from_name"] = [name]  # several editions share this address: the display name separates them
        out.append({**g, "proposed_match": match})
    return out
