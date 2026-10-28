# Architecture Decision Records

One file per decision. Status: Proposed, Accepted, Superseded by ADR-NNNN.

| ADR | Decision | Status |
|---|---|---|
| [0002](0002-llm-router.md) | Own thin router with role-based config, no LiteLLM | Accepted |
| [0003](0003-graqle-config-generated.md) | `graqle.yaml` generated from `models.yaml` | Accepted |
| [0004](0004-source-subscriptions.md) | Subscriptions as config entries, managed via MCP tools | Accepted |
| [0005](0005-mcp-sdk-v2.md) | Target `mcp` 2.x (`MCPServer`), stdio first | Accepted |
| [0006](0006-call-log-sqlite.md) | LLM call log in SQLite with input hash | Accepted |
| [0007](0007-m365-mail-via-graph.md) | Microsoft 365 mail via Graph (device code, keychain), `from_name` matching | Accepted |
| [0008](0008-gmail-imap-and-alphasignal.md) | Gmail over IMAP (app password, read-only); AlphaSignal URLs from its public web version | Accepted |
| [0009](0009-corpus-ingest-backfill.md) | Corpus file format, dedupe, ingest cursor, web-archive backfill | Accepted |
| [0010](0010-extraction-signal-store.md) | Local-model extraction, rebuildable signal store, trend maths, Graqle push | Accepted |
| [0011](0011-static-first-site.md) | Static-first site: console + public editions, computed themes, grounded Ask | Accepted |
| [0012](0012-rss-adapter-european-sources.md) | RSS adapter pulled forward from Phase 6; EU AI Act Newsletter, Sifted, Tech.eu, EC digital strategy | Accepted |
| [0013](0013-model-selection.md) | Model catalog, ranked candidates with fallback, budget cap, evaluation on our own tasks | Accepted |
| [0014](0014-hosting-public-edition.md) | Public edition on Cloudflare Pages behind Access (email PIN); jobs on the Mac via launchd | Accepted |
| [0015](0015-alphasignal-terms-compliance.md) | AlphaSignal from the email only (no site requests); left out of the public edition via a separate public store | Accepted |
| [0016](0016-bay-area-letters.md) | Bay Area Letters added: email only, links never followed, private only; IMAP sender search on the server | Accepted |
| [0017](0017-neo4j-graph-store.md) | Neo4j as the serving store for the Graqle graph: build the file, then sync an exact copy (restricted user) | Accepted |
| [0018](0018-graph-signals.md) | Graph signals from Neo4j in the weekly brief: story clusters, lens links, sector aliases | Accepted |
| [0019](0019-vector-search.md) | Vector search in Neo4j (own 384-dim index), hybrid Ask with RRF, in-process fastembed embeddings, MCP semantic_search | Accepted |
