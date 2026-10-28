"""The one interface every source implements: fetch(since) -> list[Item]."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..items import Item


@dataclass
class FetchReport:
    """Items plus enough bookkeeping to tell "nothing new" apart from "the match rule is wrong"."""

    source_id: str
    since: datetime
    items: list[Item] = field(default_factory=list)
    scanned: int = 0   # raw units looked at (emails in the folder, feed entries)
    matched: int = 0   # units that belong to this source
    skipped: list[str] = field(default_factory=list)   # matched but deliberately not news (e.g. weekend essay)
    deferred: int = 0  # matched issues to retry on a later run (e.g. link resolution failed)
    deferred_dates: list[datetime] = field(default_factory=list)  # their received times, to hold the cursor
    warnings: list[str] = field(default_factory=list)


class SourceAdapter(Protocol):
    source_id: str

    def fetch_report(self, since: datetime) -> FetchReport: ...

    def fetch(self, since: datetime) -> list[Item]: ...


class AdapterBase:
    source_id: str

    def fetch_report(self, since: datetime) -> FetchReport:  # pragma: no cover - abstract
        raise NotImplementedError

    def fetch(self, since: datetime) -> list[Item]:
        return self.fetch_report(since).items
