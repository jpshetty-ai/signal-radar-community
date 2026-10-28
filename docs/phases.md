# Phase reports

## Vector search: Neo4j index, hybrid Ask, MCP semantic_search (2026-10-22, ADR-0019)

### Works (tested)
- `embedding` role: fastembed / BAAI/bge-small-en-v1.5 in process (384 dims, 67 MB, no Ollama, no PyTorch).
- `signal-radar graph embed`: 6,565 texts (4,631 items, 1,934 entities) in 191 s; cached by text hash in
  `data/vectors.sqlite`; bounded (`--max-chunks`, `--max-minutes`); re-run embeds and writes nothing; daily runs it
  after the sync (10-minute cap). Vectors survive `graph sync`.
- Neo4j index `signalradar_chunk_vec` (cosine, ONLINE 100%); recall 19-20 of the exact top 20.
- `signal-radar search "..." [--kind item|entity] [--local]`; MCP tool `semantic_search` (read-only, typed hits,
  one graph hop of context, local fallback).
- Ask is hybrid (FTS5 + vectors, reciprocal rank fusion): blind-judged precision@12 on the 8 eval questions
  56% -> 72%, never worse. 183 tests.

### Gaps
- graq's own reasoning cannot use the vectors yet (different embedder and index size).
- Relevance judgements by one grader; questions where the corpus lacks items stay weak (T24).

## Graph signals: story clusters, lens links, sector aliases (2026-10-21, ADR-0018)

### Works (tested)
- Sector aliases: about 20 spellings merged into Financial services (37 mentions), Fintech, Payments, Insurance.
  Financial services now shows as a rising signal (W43: 9 mentions vs 2.5/week, high confidence), invisible before.
- `graph_signals.story_clusters` (Cypher on Neo4j): forming links grouped into stories by shared entity and shared
  articles; hubs do not chain stories. W43: "Microsoft + Maia 300, Nvidia", "Gemini 4 Argon + Google", ...
- `graph_signals.lens_links`: articles that mention a signal with a lens target (`lens_targets` in taxonomy.yaml).
  Every brief signal carries its link or "none"; the prompt allows a reader-specific "so what" only with a link,
  and a guard drops any sector claim on an unlinked signal (kept in `dropped_lens_claims` for audit). W43: 0 dropped.
- Brief (Markdown, console, public) shows stories and lens links; without Neo4j it falls back to pairs and says so.
- Public edition: the brief is computed from the public store, so no cluster or link rests on an excluded source;
  summaries are now dropped from the whole public brief (the deploy check caught lens evidence carrying them).
- Tests never reach the live Neo4j (conftest). 9 new tests with a fake graph; 177 in total.

### Gaps
- The writer still skips most "so what" lines (10 of 27 in W43, same as before this change).
- Lens coverage is thin: 12 of 21 lens targets (DORA, GDPR, MiCA, Basel, EBA, FCA, ECB, NIST, Model risk, ...)
  have fewer than 2 mentions and are not in the graph. One-article links are shown with their count.
- Places typed as Industry (Europe, UK, China) still rise as signals; for the views discussion (geography).
- Clusters made only of hubs ("Anthropic + Claude, AI agents") are real co-mention growth but say little.

## Neo4j serving store for the Graqle graph (2026-10-21, ADR-0017)

### Works (tested)
- `config/graph.yaml` chooses where graq reads the graph (now `neo4j`); `graqle.yaml` is generated from it, with a
  `${SIGNAL_RADAR_NEO4J_PASSWORD}` reference only. The JSON file stays graq's fallback.
- `signal-radar graph sync [--dry-run]`: projects graqle.json the way graq's `to_neo4j` does, deletes what the
  file no longer has, verifies counts and id sets. Live: 15,537 nodes, 16,351 relationships, 15,536 chunks in
  4 s; a second run and a run after a fresh rebuild both had nothing to change.
- Neo4j user `signal_radar` (database `signalradar` only; access to `neo4j` refused, verified). Password in the
  keychain; the daily job finds it without shell variables (tested in launchd's environment).
- Daily: build, then sync (`sync.daily`); a failed build skips the sync. `signal-radar graq-mcp` starts the graq
  MCP server with the password; it reports `neo4j://bolt://localhost:7687` (`.mcp.json`).
- 12 tests (in-memory fake database): first fill, dry run, stale deletes, stable edge ids, mass-delete and empty
  guards, mismatch, timeout then convergence, password source, daily step. 168 tests in total.

### Gaps
- graq version split: the daily build uses /opt/homebrew/bin/graq 0.68.1; the venv's 0.85.0 runs unlicensed with
  a 500-node cap (grace ended 2026-10-11), so `signal-radar graph` from a shell with the venv on PATH fails and
  deletes the file first. On 2026-10-21 that happened once; the file was restored byte-identical from the import
  copy. Register or license graq 0.85 and use one version everywhere.
- The graph build (graph_export.push) still deletes the file before scanning, so any failed build leaves no file
  until the next good one. Neo4j keeps its copy meanwhile (the sync skips after a failed build).
- `graph.connector` is deprecated in graq 0.85 (its `backends:` section is not yet read at runtime).
- The sync runs on the shared Neo4j instance; a Neo4j outage makes the daily run report an error (data run stands).

## Bay Area Letters (2026-10-09, ADR-0016)

### Works (tested)
- `parser: bayarealetters` (h3 + "TL;DR"), read from `[Gmail]/All Mail` with a server-side sender search; items
  link to the issue in Gmail. 67 issues since 2026-07-06 ingested (886 stories, 0 skipped). Private only.
- IMAP `list_messages(senders=...)` searches FROM on the server for every email source. 153 tests.

### Gaps
- No article URLs, so no cross-source merge with TL;DR (T26). No published terms: never public without permission.

## Before a wider audience: analytics, feedback, privacy (2026-10-05)

### Works (tested)
- `config/publish.yaml`: `web_analytics_token` (Cloudflare Web Analytics beacon, cookieless), `feedback_url`
  (footer link) and `privacy_contact`. Each shows only in the public edition and only when set.
- Public Method page has a GDPR privacy note (controller, login data via Cloudflare Access, the one login cookie,
  analytics and feedback when configured, transfers, rights, contact). Every public footer links to it.

### Gaps
- Web Analytics site not created yet: the API token has no Web Analytics permission, so it is added once in the
  dashboard. Feedback form and privacy contact not chosen yet.
- The privacy note is a plain-language draft, not legal advice.

## AlphaSignal terms compliance (2026-10-05, ADR-0015)

### Works (tested)
- The AlphaSignal parser reads the email only; items link to the issue page, built from the id, never fetched.
  All 12 real samples parse email-only; a test fails on any network call during parsing.
- The public edition and the single-file share are built from `data/signals_public.sqlite` (corpus without
  `exclude_sources`): 2,453 items, 472 entities, no AlphaSignal item, mention or co-coverage. The deploy bundle check
  refuses any file naming an excluded source. `signal-radar brief` writes a separate public brief.
- 145 tests.

### Gaps
- New AlphaSignal items no longer merge with the same story in TL;DR (no article URL): co-coverage is lost for them.
- explore.js still carries the AlphaSignal display label in its source-name map (code, not data).

## Phase 6b: hosting of the public edition + launchd scheduling (2026-10-04, ADR-0014)

### Works (tested)
- `signal-radar deploy`: builds the public edition, checks the bundle (path allowlist, no `summary`/`vote` fields,
  no console script, Pages limits), refuses to upload unless a Cloudflare Access app covers `<project>.pages.dev`
  and `*.<project>.pages.dev` with email-only allow policies, uploads with wrangler, then probes both URLs
  anonymously and fails if either answers without a login. `--dry-run` prints the plan; `--setup` creates the
  Pages project, one-time PIN login, viewer policy and Access app (idempotent).
- Real dry run: 558 files, 1.7 MB (550 entity files), no problems found.
- Public pages carry the publisher footer from `config/publish.yaml`, `noindex`, and no longer ship `console.js`.
- `signal-radar daily` ends with an optional publish (`daily: false` by default); a failed publish is reported
  and sets exit code 1 without undoing the data run.
- `signal-radar schedule install|uninstall|status|run daily`: launchd LaunchAgent, log in `data/logs/daily.log`.
- 9 tests (`tests/test_publish.py`) against an in-memory Cloudflare API: bundle guard, viewer list, idempotent
  setup, access gaps (missing wildcard, non-email rule), refuse-before-upload, loud failure on an open URL, plist.

- Ran live since 2026-10-04 behind Access (email PIN, a handful of invited viewers). `deploy --setup` worked first time against the real API. Anonymous requests to the page and
  `data/snapshot.json` on the production and per-deployment hostnames all redirect to the login (302); the user
  logged in from a private window and saw the site. `daily: true` set after that check.
- A new per-deployment hostname fails TLS for about 45 s after upload (certificate issuance); the probe reports
  it as unverified instead of failing.

### Untested / gaps
- The launchd job needs the Mac on once a day and Ollama running. Since 2026-10-05 it also runs at login with
  `--catch-up` (skips if the last successful run is under 20 h old), so a shutdown no longer skips a day.
- A non-listed email was not tested live (covered by the policy check: only the 3 listed addresses are admitted).
- Weekly brief: since 2026-10-15 `daily` writes it. The first successful run of each ISO week builds both editions
  (window Tuesday to Monday) before the site build and deploy, so a Mac that was off on Monday catches up. No
  separate launchd job: one would race `daily` at login and deploy twice. If the console edition is written but the
  public one fails, the next run does not retry it: run `signal-radar brief --end <Monday>` by hand.
- Footer text is a default, not legal advice: terms of use, a viewer privacy notice and the copyright position
  of headlines are open before any wider audience.
- The Method page describes the reader's lens and sources in detail; fine for invited readers, review before
  any wider audience.

## Phase 5c: weekly signal brief (2026-10-04)

### Works (tested)
- `signal-radar brief`: computed sections (rising with medium/high confidence, watching, new this week, fading,
  clusters forming, items through the lens, coverage and watchlist) from the signal store, then one `writing`
  call for the prose: headline, a "so what" per signal, up to 2 learning items, up to 2 opportunity ideas.
  Prose that names an unknown signal or cites an unknown item is dropped; house style enforced.
- Outputs: briefs/<ISO week>.md (Git-tracked) and data/briefs/<week>.json; a Brief page in the console, the public
  edition (without newsletter summaries) and the single-file share.
- MCP: resource `brief://latest`; prompts `weekly_signal_brief`, `learning_radar`, `content_ideas` (the three
  prompts from CLAUDE.md), which set up the task and name the tools; they compute nothing themselves.
- First brief (2026-W40) built with the local fallback in about 1 minute. 113 tests.

### Gaps
- Written by local qwen3:8b until the model evaluation picks a writer: it skips some "so what" lines and forces the
  financial-services angle in places (e.g. claims about "regulated sectors" no item makes).
- 7-day counts are small (most risers have 3 mentions): the brief separates "rising" (medium/high confidence)
  from "watching". Extracted Skill entities are unusable, so the learning radar works from rising technologies.
- Scheduled through `daily` since 2026-10-15 (see Phase 6). W42 was written by hand after a 10-day gap.
- Entity noise still reaches "rising": W42 lists "Workers" as a Person and "text" as a Technology.

## Phase 5b: model selection (2026-10-04, ADR-0013)

### Works (tested)
- Provider profiles by kind, OpenRouter with no-retention routing; model catalog (11 models, Oct 2026 prices);
  writing and reasoning roles as ranked candidates ending in the local model; `--model role=@catalog_id`.
- Router falls through candidates, skips a provider without a key once per run, enforces `budget.monthly_usd`.
  Verified live: with no OpenRouter key the writing role answers with local qwen3:8b.
- `signal-radar models eval`: tasks from the real signal store, automatic scoring, optional blind judge, cost
  estimate + cap + confirmation, report and scores file feeding `cheapest_passing`. Local-only trial run: 11 tasks,
  2.5 min, $0. Estimate for the full candidate set with a Sonnet judge: about $1.17.
- Narration cache keyed by preferred candidate (a model change regenerates text).
- Judges: catalog model, Jev (verified live: two outputs judged for $0.00018, one title flagged "overstated"),
  and blind grading sheets (`models blind` / `models grades`) with judge agreement. 108 tests.

### Untested / gaps
- No cloud model has run yet: waiting for an OpenRouter key and approval of the evaluation spend.
- Automatic scores rate the local model ~0.95; only the judge separates subtle overstatement. The 0.85 bar is a
  starting point to calibrate after the first judged run.
- Ask is still retrieval + one call; a tool-calling Ask over the MCP tools comes after model selection.

## Phase 6a (pulled forward): RSS adapter + European sources (2026-10-04, ADR-0012)

### Works (tested)
- `rss` sources are fetched by `FeedAdapter`: date filter on the cursor, dedupe, canonical URLs, section from the
  feed category, summary fallback chain (description, content lead, page `og:description`), `strip` regexes.
  7 offline tests in `tests/test_feed.py`.
- Live: EU AI Act Newsletter (8 items since July), Sifted (24), Tech.eu (20), EC digital strategy (10) ingested;
  a re-run writes nothing new.
- Console labels for the new sources.

### Untested / gaps
- No scheduler yet. Sifted and Tech.eu keep 3 or 4 days of entries, so run `signal-radar daily` at least every
  2 days or items are lost.
- EU AI Act Newsletter issues are digests: one item, one-sentence summary, many stories. Entities are under-counted.
- European entities have no history, so they show as "new" movers for about four windows.
- `subscribe_source` cannot set `edition`, `page_meta` or `strip`; `import_opml` is still not built.

## Phase 5a: desktop console + static site (2026-10-04, ADR-0011)

### Works (tested)
- `signal-radar web` builds and serves the console at http://127.0.0.1:8765 (local only).
  Today: three themes (one lens theme, two momentum themes) with model-written title, summary and "why it
  matters", evidence and confidence; ranked reading list (7 days) with +/- feedback; rising and fading entities
  with 12-week sparklines; blind spots (lens categories, regulatory watchlist); market map.
  Explore: search and type filter over 536 entities, radial neighbourhood graph, entity panel with sparkline,
  validated relations and news. Method: how every number is made.
  Ask: retrieval-grounded answers with citations and a visible trace (local qwen3:8b, about 20 s).
- `signal-radar site build --edition public` renders the same site without newsletter summaries, Ask or feedback.
- `daily` now also rebuilds the console site. Light and dark themes checked with headless Chrome screenshots.
- 103 tests (site: themes, narration fallback and cache, Ask grounding, editions, API routes).

### Untested / gaps
- The theme cards are written by the local model: one summary said "breached" where the source said "hit",
  and "why it matters" forces a financial-services angle even when the link is weak. A stronger writing model
  needs an API key and approval.
- Feedback is stored and hides down-voted items, but does not yet re-rank (learning from votes is next).
- The explorer's date window is fixed at 90 days (no slider yet). Entities with fewer than 3 mentions have no page.
- Fonts load from Google Fonts, so the console needs internet for its typefaces (falls back to system fonts).
- No scheduler yet: run `signal-radar daily` by hand until Phase 6 (launchd).

## Phase 4: extraction + signal store (2026-10-04, built while the user was away; see ADR-0010)

### Works (tested)
- Extraction of all 2,758 items with local qwen3:8b: 0 failures, about 11 s per item (8.7 h first run, newest
  first). Raw JSON stored per item; resumable; stale items detected by input hash.
- Prompt v3 after a 300-item review. v2 rated 57% of items relevant (2-3) and copied the reader's interests into
  categories. v3: 0: 647, 1: 1,964, 2: 132, 3: 15.
- Signal store rebuild (about 1 s, no LLM calls): 6,009 entities, 10,468 mentions, 2,687 relations. Rules dropped
  587 generic and 476 not-named entities and 1,537 ill-typed relations, and flipped 187 reversed ones. Aliases +
  plural + brand-prefix merges ("Opus 5.5" -> "Claude Opus 5.5").
- Metrics: movers/fading (14-day default; 7-day counts are thin at about 40 items/day), `--min-relevance` to cut
  off-topic movers, trend series, co-occurrence clusters, reading list with same-story suppression across editions,
  category counts. CLI `signal-radar signals ...`; MCP `top_movers`, `trend_metrics`, `emerging_clusters`,
  `reading_list`, `extract_entities`.
- Graqle: 2,758 document nodes + 1,118 entity nodes (>= 2 mentions) + 6,257 edges in data/graqle/graqle.json,
  rebuilt from scratch on every push (3 s). `signal-radar daily` = ingest + extract + rebuild + graph.
- 95 tests.

### What the first run shows (as of 2026-10-02)
- Rising (14 d, relevance >= 1): Claude Opus 5.5 (new, high), Meta Muse, TypeSafe Jev and decision models
  (both sources, high), GPT-6 Sol/Luna (new), Claude Code. Fading: Nvidia, DeepSeek, Z.ai, Tesla.
- Reading list for the last 7 days is dominated by AI model risk and safety incidents (OpenAI pausing training
  after a sandbox escape, GPT-6 Astra's unsanctioned attacks in simulation, the OpenAI/Anthropic incident probes,
  agents on US government sites): directly relevant to model risk work.

### Gaps (honest)
- **Source coverage for the lens is the biggest gap.** "EU AI Act" does not appear once in 90 days. Only 11 of
  about 870 items in the last 28 days are regulation/governance and 6 financial services. The radar sees AI tooling
  well and the user's core market barely. Regulatory and FS sources are needed (Phase 6, RSS).
- 30% of items are "other", mostly off-topic Founders/Tech items. The model rarely uses `business_strategy`.
- Relevance is now strict (0.5% score 3) and still occasionally mentions financial services when the item doesn't.
- Entity typing is noisy for Industry ("Texas", "businesses") and Concept. Relations are weak (most dropped).
- Movers at 14 days rest on 3-10 mentions: confidence labels say so; treat "low" as a hint.
- Same story in two editions with different titles: suppressed in the reading list, but counted twice in mentions.

## Phase 3: corpus, ingest, backfill (2026-10-03, built while the user was away; see assumptions in ADR-0009)

### Works (tested)
- Corpus writer: one Markdown file per item, three-way dedupe (id, url across sources -> `also_in`, same-day
  title for short-link copies). 2,758 items: TLDR AI/Tech/Founders for 90 days from the web archive, AlphaSignal
  from 2026-07 from Gmail. 69 AlphaSignal stories were already in the corpus from TL;DR and became `also_in`
  (cross-source corroboration).
- `signal-radar ingest`: per-source cursor with 2-day overlap; a deferred issue holds the cursor; one failing
  source doesn't stop the others. `signal-radar backfill`: polite (2 s), cached, remembers no-issue days.
- MCP: `run_ingest`, `ingest_items`, resources `corpus://items/{id}` and `corpus://digest/{date}`.
- `graq scan docs` verified on corpus samples (document nodes keyed by item id, no LLM calls).

### Bugs found and fixed during the build
- `corpus or Corpus()` treated an *empty* corpus as missing (Corpus has `__len__`), so tests wrote 5 synthetic
  items into the real corpus. Fixed everywhere (`is None` checks); the 5 files were identified by their fake
  origin ids and removed; tests now verify the real corpus is untouched.
- Double-escaped ampersands (`&amp;amp;`) left `amp;utm_source` in 20 URLs. `canonical_url` decodes entities;
  the 20 files were migrated to their corrected id (19 moved, 1 true duplicate removed).

### Untested / gaps
- Daily ingest against Gmail TL;DR (first issue Monday). AlphaSignal ingest from Gmail is live-verified.
- 1 AlphaSignal issue (2026-08-01, special edition without a web version) stays deferred, so the AlphaSignal
  cursor is held at that date. Each daily run re-fetches from there (cheap, dedupes); see the Phase 4 notes.
- Graqle: its section nodes link to `doc::<file>` ids that don't exist (Graqle-side quirk); embeddings fall back
  to bag-of-words because `sentence-transformers` is not installed.

## Phase 2b: Gmail (IMAP) and AlphaSignal (2026-10-03)

### Works (tested)
- Gmail over IMAP with an app password from the macOS keychain (ADR-0008): read-only (EXAMINE + BODY.PEEK), day-level
  SEARCH SINCE with an exact INTERNALDATE cut, typed auth/folder errors. Verified live on the "Newsletters" label.
- AlphaSignal parser: sections, engagement counts, sponsors dropped (banner, feature and in-Signals), Sunday Deep
  Dives skipped, real URLs from the anonymous public web version, tracker URLs never requested. All 12 real issues
  (2026-09-20 .. 10-02) pass; live: 12 issues -> 79 items, 2 skipped, 0 deferred. Also verified through MCP
  Inspector (`fetch_source alphasignal`).
- Adapter reports `skipped` and `deferred`; `fetch_source` returns both. 76 tests pass.
- TL;DR sources now read `gmail:Newsletters`.

### Untested
- TL;DR parsing from Gmail: no issue has arrived there yet (signup on 2026-10-03, first issue expected Monday).
  The parser is the same; the MIME body from Gmail may differ from Graph's HTML, as it did once before.
- Behaviour when Google revokes the app password (expect MailAuthRequired with a clear message).

### Known gaps
- Engagement numbers are AlphaSignal's, undocumented; treat as an attention hint.
- Signals entries have no summary (AlphaSignal gives only a one-line title), so extraction has less text to work with.
- The work mailbox still receives TL;DR; unsubscribe it there to stop the noise (no source reads it).

## Phase 2: signal-sources-mcp with the TL;DR email adapter (2026-10-03)

### Works (tested)
- Microsoft 365 mail through Graph (ADR-0007): device code sign-in (`signal-radar auth work`), MSAL token cache in
  the macOS keychain, silent refresh. Verified live against the work mailbox.
- Folder resolution for a top-level name containing `/` (what the Outlook rule created) and for nested paths;
  read-only listing with paging, 429/503 retry, typed errors (`MailAuthRequired`, `MailError`).
- `EmailMatch.from_name` (exact display name): separates TL;DR editions that share one sender address.
- TL;DR parser: edition and issue date, sections, read time, content kind ("GitHub Repo"), tracking links unwrapped
  locally, `utm_*` stripped, sponsors and TL;DR's own links dropped, duplicates removed. All 7 real editions received
  on 2026-10-02 parse to 12-15 items (AI, IT, Founders, Design, Dev, DevOps, Marketing).
- Adapter reports `scanned` / `matched` / warnings, so "wrong match rule" and "template changed" show up instead of
  silently returning nothing (both happened during this phase and were caught this way).
- MCP server `signal-sources-mcp` (stdio): `list_sources`, `fetch_source`, `suggest_newsletter_filter`,
  `subscribe_source`, `unsubscribe_source`, with output schemas and tool annotations. Tested in-process with
  `mcp.Client`, over stdio with MCP Inspector (CLI mode), including a live `fetch_source` against the mailbox.
- CLI: `auth`, `fetch`, `mail-senders` (with `--save-html` for parser samples). 57 tests pass.

### Untested
- MCP Inspector web UI (only its CLI mode was used); Claude Desktop / Claude Code registration of the server.
- Token expiry and refresh after the access token's 1 hour lifetime in a long-running server process (MSAL should
  refresh silently; not yet observed).
- `subscribe_source` for rss against a live feed (probe code path only covered by input validation tests).
- The flagship "TLDR" edition: not received yet, so `tldr_tech` matches nothing today (it warns).

### Known gaps
- `links.tldrnewsletter.com/<code>` short links (0-2 per issue) are not resolved, so cross-edition dedupe misses them.
- Same story in two editions with different titles gets two ids (dedupe is url + title by design).
- No fetch cursor yet: `fetch_source` takes `since` explicitly. Idempotent ingest with a cursor arrives with the
  corpus writer in Phase 3 (`ingest_items`), as do the `corpus://` resources and the prompts.
- `import_opml` (ADR-0004) moved to Phase 6 with the rss adapter.
- IMAP accounts are in the schema but have no reader.

## Phase 1: scaffold, config, llm router (2026-10-02)

### Works (tested)
- `config/models.yaml` and `config/sources.yaml` load into strict pydantic models; typos and unknown keys fail at load.
- Role routing with per-run `--model role=provider/model` overrides (model names containing `/` handled).
- Provider adapters: Ollama (native HTTP), Anthropic (SDK 1.11), OpenAI/compatible (SDK). Request shapes verified with
  fakes and an `httpx.MockTransport`; Anthropic kwargs (`output_config`, `fallbacks`, `betas`) verified against the
  installed SDK's signature.
- Cost per call from the price table, priced by the model that actually served; unknown prices logged as NULL.
- Call log with input hash and a cross-model comparison query.
- `graqle.yaml` generated from `models.yaml`, validated against Graqle 0.85's own schema; `graq config` reads it.
- 25 unit tests pass.

### Untested
- No live call to any provider: this environment has no Ollama and no API key. First thing to run locally:
  `signal-radar llm extraction "ping"` and `signal-radar llm reasoning "ping"`.
- `graq scan` / `graq mcp serve` with this config (Phase 3).
- OpenAI structured output with `strict: true` requires every property in `required`; schemas are not checked for that yet.

### Known gaps
- No streaming path; writing jobs above ~16k output tokens will need one.
- Graqle and our embedding role use different embedding models (ADR-0003).
- TL;DR sender address in `sources.yaml` is a domain substring guess; confirm against a real message in Phase 2.
