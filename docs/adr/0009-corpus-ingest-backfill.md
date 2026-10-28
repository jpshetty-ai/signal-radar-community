# ADR-0009: Corpus format, ingest cursor and web-archive backfill

**Status:** Accepted (2026-10-03, decided autonomously while the user was away; revisit)

## Context
Phase 3 turns fetched items into a durable corpus that everything else (signal store, Graqle, website) is
rebuilt from. Trend maths needs history, and email only starts the day you subscribe.

## Decision
- **File per item:** `corpus/YYYY/MM/<date>-<id>.md`, YAML frontmatter (id, date, source, edition, url, title,
  section, tags, read_minutes, content_kind, engagement, origin_ref, ingested_at, also_in) and the summary. Git-tracked.
- **Dedupe**, in order: same id (canonical url + title); same canonical url from another source, recorded in the
  existing file's `also_in`; same source + date + normalised title (email copy with a short link vs archive copy
  with the real URL). `also_in` is kept on purpose: two independent curators covering a story is evidence.
- **Cursor** per source in `data/ingest_state.json`. Each run fetches from cursor minus 2 days (dedupe makes
  the overlap free). A deferred issue holds the cursor at its date so it is retried.
- **Backfill from public web archives**, configured per source (`archive: {url: ...{date}, parser: ...}`):
  TL;DR at `tldr.tech/<edition>/<date>`. robots.txt allows these paths, and the terms say nothing about automated
  access. Fetched at 1 request / 2 s with a plain User-Agent, personal use. Raw pages are cached in
  `data/cache/<source>/` and never re-fetched. A day without an issue (404, or the signup page TL;DR Founders
  serves on Tue/Thu) is remembered as `.missing`.
- The archive is preferred for history because it has direct links (no tracker) and resolves TL;DR short links.

## Consequences
- Corpus of 2,759 items after the first backfill (90 days TL;DR AI/Tech/Founders, AlphaSignal from 2026-07).
- One corpus file per item means thousands of small files in Git. Fine at this scale (a few MB a quarter).
- If tldr.tech changes its page layout, backfill warns "page parsed to 0 items" instead of silently skipping.
