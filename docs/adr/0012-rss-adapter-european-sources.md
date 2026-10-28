# ADR-0012: RSS adapter pulled forward; first European sources

**Date:** 2026-10-04 | **Status:** Accepted

## Context
All four sources (TL;DR AI, Tech, Founders and AlphaSignal) are US curators. The user's market is EU financial services,
risk, compliance and the EU AI Act, which those sources barely cover (Phase 4 gap). The candidate European sources
publish RSS. Email subscriptions would need one parser per newsletter; one RSS adapter serves every feed and makes
subscribing a config entry (ADR-0004). So the `rss` adapter moves from Phase 6 into now. HN, arXiv, GitHub and
scheduling stay in Phase 6.

## Decision
- `sources/feed.py`: `FeedAdapter(FeedSource)` implements `fetch_report(since)` with feedparser. Feed and page
  fetchers are injected callables, so tests run offline.
- Item mapping: `date` = entry published (else updated, else today with a warning); `section` = first category that
  is not "General"/"Uncategorized"; `edition` = config `edition` or the feed title; `origin_ref` = entry guid.
- Summary, in order: feed description; if that is a CMS auto-excerpt cut with "..." (WordPress), or empty, a short
  lead from `content:encoded`; if still empty and `page_meta: true`, the article page's `og:description`, read only
  up to `</head>` (Sifted pages are ~3 MB). Capped at `max_summary_chars` (700), cut at a word. Never full text.
- `strip`: per-source regexes for CMS boilerplate (Drupal author/date lines, a podcast intro repeated on every
  episode). Config, not code, so the next odd feed needs no release.
- The adapter warns when a feed does not reach back to `since`: those entries rolled off and are lost.
- Source ids are chosen so the prefix before `_` is a distinct family (`signals._family`): `aiact_newsletter`,
  `sifted`, `techeu`, `ec_digital`. Each counts as independent evidence in `top_movers` confidence.

Sources added: EU AI Act Newsletter (Future of Life Institute, Substack), Sifted (FT-owned), Tech.eu, and the
European Commission's "Shaping Europe's digital future" feed.

## Consequences
- Busy feeds keep only 3 or 4 days of entries (Sifted 24 entries, Tech.eu 20). Until Phase 6 scheduling exists,
  `signal-radar daily` must run at least every 2 days or items are silently lost (the warning tells you after the fact).
- Sifted costs one extra HTTP request per new entry (head only, about 24 a day). robots.txt allows it.
- The EU AI Act Newsletter is a digest: one feed item covers many stories, so its one-sentence description
  under-counts entities. Paid-only posts show the free preview. Splitting issues into stories needs a parser (later).
- The EC feed mixes news, consultations and events; events are low-signal but low volume (about 10 entries over 2 months).
- New European entities have no baseline, so for about four windows they appear as "new" movers. Read
  European movers with that in mind until roughly December 2026.
- `subscribe_source` cannot yet set `edition`, `page_meta` or `strip`; edit sources.yaml for those.
