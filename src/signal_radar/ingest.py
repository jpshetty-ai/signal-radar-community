"""Daily ingest and history backfill: sources -> corpus, with a per-source cursor.

Cursor rule: the next run fetches from (cursor - overlap). The corpus dedupes, so overlap costs nothing and
protects against late-arriving mail. A deferred issue holds the cursor at its own date so it is retried.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from .config import DATA_DIR, EmailNewsletterSource, SourcesConfig, load_sources
from .corpus import Corpus
from .sources import MailFactory, build_adapter, default_mail_factory
from .sources.parsers.tldr_web import edition_from_page, parse_tldr_web

STATE_PATH = DATA_DIR / "ingest_state.json"
CACHE_DIR = DATA_DIR / "cache"
OVERLAP = timedelta(days=2)
MAX_DEFERRAL_HOLD = timedelta(days=7)  # a deferred issue stops holding the cursor after this
FIRST_RUN_LOOKBACK = timedelta(days=7)
ARCHIVE_PARSERS = {"tldr_web": parse_tldr_web}


@dataclass
class SourceRun:
    source_id: str
    since: str
    scanned: int = 0
    matched: int = 0
    fetched: int = 0
    written: int = 0
    duplicates: int = 0
    merged: int = 0
    skipped: list[str] = field(default_factory=list)
    deferred: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class IngestState:
    """Per-source cursor (ISO timestamp of the newest fully processed message) in data/ingest_state.json."""

    def __init__(self, path: Path | None = None):
        self.path = path or STATE_PATH
        self.data: dict = json.loads(self.path.read_text()) if self.path.exists() else {}

    def cursor(self, source_id: str) -> datetime | None:
        v = self.data.get(source_id, {}).get("cursor")
        return datetime.fromisoformat(v) if v else None

    def set_cursor(self, source_id: str, when: datetime) -> None:
        self.data.setdefault(source_id, {})["cursor"] = when.astimezone(UTC).isoformat()
        self.data[source_id]["updated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)


def run_ingest(
    source_ids: list[str] | None = None,
    since: datetime | None = None,
    *,
    cfg: SourcesConfig | None = None,
    corpus: Corpus | None = None,
    state: IngestState | None = None,
    mail_factory: MailFactory = default_mail_factory,
    now: datetime | None = None,
) -> list[SourceRun]:
    """Fetch each enabled source (or the given ones) and write new items to the corpus."""
    cfg = cfg if cfg is not None else load_sources()
    corpus = corpus if corpus is not None else Corpus()
    state = state if state is not None else IngestState()
    now = now if now is not None else datetime.now(UTC)
    sources = [cfg.get(s) for s in source_ids] if source_ids else [s for s in cfg.sources if s.enabled]
    runs: list[SourceRun] = []
    for source in sources:
        cursor = state.cursor(source.id)
        start = since or ((cursor - OVERLAP) if cursor else now - FIRST_RUN_LOOKBACK)
        run = SourceRun(source_id=source.id, since=start.isoformat())
        try:
            report = build_adapter(source, cfg, mail_factory).fetch_report(start)
        except Exception as e:  # noqa: BLE001 - one broken source must not stop the others
            run.error = f"{type(e).__name__}: {e}"
            runs.append(run)
            continue
        result = corpus.write(report.items)
        run.scanned, run.matched, run.fetched = report.scanned, report.matched, len(report.items)
        run.written, run.duplicates, run.merged = len(result.written), result.duplicates, len(result.merged)
        run.skipped, run.deferred, run.warnings = report.skipped, report.deferred, report.warnings
        # Hold the cursor at the oldest deferred issue; otherwise advance to now.
        if report.deferred_dates:
            state.set_cursor(source.id, max(min(report.deferred_dates), now - MAX_DEFERRAL_HOLD))
        else:
            state.set_cursor(source.id, now)
        runs.append(run)
    state.save()
    return runs


# ------------------------------------------------------------------ backfill


def _weekdays(start: date, end: date, weekdays_only: bool):
    d = start
    while d <= end:
        if not weekdays_only or d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def default_page_fetch(url: str) -> str | None:
    """GET a public archive page. None for 404 (no issue that day)."""
    r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "signal-radar/0.2 (personal research)"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


def backfill_archive(
    source_id: str,
    days: int,
    *,
    cfg: SourcesConfig | None = None,
    corpus: Corpus | None = None,
    fetch: Callable[[str], str | None] = default_page_fetch,
    delay_s: float = 2.0,
    cache_dir: Path | None = None,
    today: date | None = None,
    log: Callable[[str], None] = lambda m: None,
) -> SourceRun:
    """Fill the corpus from a source's public web archive, one page per issue day, politely.

    Raw pages are cached under data/cache/<source>/ so re-parsing never re-fetches.
    """
    cfg = cfg if cfg is not None else load_sources()
    source = cfg.get(source_id)
    if not isinstance(source, EmailNewsletterSource) or source.archive is None:
        raise ValueError(f"source {source_id!r} has no web archive configured")
    archive = source.archive
    parse = ARCHIVE_PARSERS[archive.parser]
    corpus = corpus if corpus is not None else Corpus()
    cache = (cache_dir or CACHE_DIR) / source_id
    cache.mkdir(parents=True, exist_ok=True)
    today = today if today is not None else datetime.now(UTC).date()
    start = today - timedelta(days=days)
    run = SourceRun(source_id=source_id, since=start.isoformat())
    for day in _weekdays(start, today, archive.weekdays_only):
        page_file = cache / f"{day.isoformat()}.html"
        missing_file = cache / f"{day.isoformat()}.missing"
        if missing_file.exists():
            continue
        if page_file.exists():
            html = page_file.read_text()
        else:
            url = archive.url.format(date=day.isoformat())
            try:
                html = fetch(url)
            except Exception as e:  # noqa: BLE001
                run.warnings.append(f"{day}: {e}")
                continue
            finally:
                time.sleep(delay_s)
            if html is None:
                missing_file.touch()
                continue
            page_file.write_text(html)
        run.scanned += 1
        items = parse(html, source_id=source_id, issue_date=day, edition=edition_from_page(html),
                      tags=source.tags, origin_ref=archive.url.format(date=day.isoformat()),
                      fetched_at=datetime.now(UTC))
        if not items:
            if "<article" not in html:
                missing_file.touch()  # e.g. TL;DR Founders has no Tue/Thu issue: the URL serves the signup page
                continue
            run.warnings.append(f"{day}: page parsed to 0 items")
            continue
        run.matched += 1
        run.fetched += len(items)
        result = corpus.write(items)
        run.written += len(result.written)
        run.duplicates += result.duplicates
        run.merged += len(result.merged)
        log(f"{source_id} {day}: {len(items)} items, {len(result.written)} new")
    return run


def run_summary(runs: list[SourceRun]) -> list[dict]:
    return [asdict(r) for r in runs]
