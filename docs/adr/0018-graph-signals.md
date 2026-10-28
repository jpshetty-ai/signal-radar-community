# ADR-0018: Graph signals from Neo4j: story clusters and lens links

**Status:** Accepted (2026-10-21)

## Context
Since ADR-0017 the Graqle graph is served from Neo4j (`signalradar`). Probes on 2026-10-21 showed what the graph
can carry and what it cannot:

- The dated co-mention structure (document MENTIONS entity, document date) is solid. Pairs mentioned together for
  the first time this month form recognisable stories: Claude with Google Slides, Docs and Sheets (4, 3 and 3
  articles, never together before 7 October) is one story, Claude moving into Google Workspace. The brief's
  "clusters forming" lists pairs one by one, so it shows that story as three unrelated rows, or not at all.
- Typed relations are too noisy to build on ("Hugging Face REGULATES Bank"). Half the Industry entities are
  places (China 47, US 30, Europe 28, UK, Texas). The reader's market is split over about 20 spellings
  (finance 10, banking 6, Bank 3, Financial Services 2, Financial firms 1, ...), none big enough to register.
- The brief asks the model for a "so what" for the reader on every signal. With no evidence of a link to
  financial services or regulation, the model invents one (Phase 5c gap: "regulated sectors" claims no item made).
- No Graph Data Science or APOC library on the shared instance; installing one restarts it for every team.

## Decision
1. **Sector aliases.** `config/aliases.yaml` merges the financial-services spellings into `Financial services`
   (and `Fintech`, `Payments`, `Insurance`). "Finance teams" (the corporate finance function) stays separate.
   Places typed as Industry are left for the views discussion (geography is not in the ontology yet).
2. **Story clusters** (`graph_signals.story_clusters`, Cypher on Neo4j):
   - a *forming link* is a pair of entities co-mentioned in at least 3 articles in the window and at least twice
     as often as in an average earlier window of the same length (four windows back), or never before;
   - links that share an entity *and* share articles (Jaccard of their article sets at least 0.3) are one story;
     a hub such as OpenAI therefore does not chain unrelated stories together;
   - a cluster carries its entities, the articles that mention two or more of them, source families and a
     confidence label (low below 4 articles or one source family).
3. **Lens links** (`graph_signals.lens_links`): for each brief signal, the articles in the last 90 days that
   mention the signal's entity together with one of the lens targets (`lens_targets` in `config/taxonomy.yaml`:
   Financial services, Fintech, Payments, Insurance, EU AI Act, AI Office, AI governance, Model risk, SEC, ...).
   Direct co-mention only: two-hop paths run through hubs and connect everything to everything.
4. **The brief uses them.** Clusters replace the pairwise list (the pairwise SQLite version stays as the
   fallback when Neo4j is unreachable, labelled as such). Each signal carries its lens link or none; the prompt
   allows a reader-specific "so what" only for signals with a link and asks for a plain one otherwise.
5. **Editions.** Neo4j holds every source. The brief passes the item ids of the store it was built from (the
   public store has no AlphaSignal or Bay Area Letters items), so a public cluster or link never rests on an
   excluded source.
6. **Targets are config, so views can follow.** A view (architecture, product, segment, geography) is a named set
   of targets plus filters; that is a separate design discussion. Only the reader's lens is built now.

## Consequences
- The brief shows stories, not pair lists, and stops inventing a financial-services angle.
- Lens links will be rare: the sources cover the reader's market thinly (Phase 4 gap). The brief says so.
- The brief depends on Neo4j for clusters and links; without it, clusters fall back to pairs and links are
  omitted (reported in the brief data as `graph: unavailable`).
- Graph algorithms (centrality, communities) stay out until a decision on GDS or Python-side computation.
