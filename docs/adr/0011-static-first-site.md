# ADR-0011: Static-first site with two editions (private console, public)

**Status:** Accepted (2026-10-04)

## Context
The user wants a desktop UI now (daily themes, a graph explorer, a chat panel) and may later publish the product:
the framework as open source, and a public site on processed data, generated daily and hosted cheaply.

## Decision
- **One builder, two editions.** `signal-radar site build` renders Jinja templates from one JSON snapshot of the
  signal store into a static folder (HTML, CSS, JS, `data/*.json`, one JSON file per entity neighbourhood).
  - `console` (default, `data/site/`): includes newsletter summaries, an Ask drawer and +/- feedback.
  - `public` (`data/site_public/`): no newsletter summaries (headlines link to the original article, plus our
    own data), no Ask, no feedback. Deployable as-is to any static host.
- **Live parts only where needed.** `signal-radar web` serves the console folder on 127.0.0.1 and adds three
  routes: `POST /api/feedback`, `GET /api/votes`, `POST /api/ask`. Everything else is the static folder.
- **Themes are computed, prose is generated.** Theme detection is deterministic (themes.py): a lens theme from
  items rated relevant (2-3), then momentum themes from rising entities grown by co-mentions, with overlap
  limited. A model only names and summarises a theme from its items and must cite them.
- **Models by role with fallback.** Narratives use `writing`, answers `reasoning`; if that role is unusable
  (no API key) they fall back to `extraction` (local). The model that wrote a text is shown next to it. Generated
  text is cached by input hash in `signals.sqlite:narrations`.
- **Ask v1 is retrieval-grounded, not agentic:** whole-word entity matches + FTS5 search over titles and
  summaries -> up to 12 items + trend series -> one model call that may only use them. The answer lists the
  items used and a trace (entities matched, terms, items read, model).
- **Explorer** uses a radial layout (selected entity, co-mentioned entities on an inner ring, items on an outer
  ring) drawn in plain SVG, no library: readable at ~25 nodes, the same picture every time.
- **Look:** "Radar" direction: Martian Mono (labels, data) + Hanken Grotesk (reading), one muted teal accent,
  confidence shown as filled dots, graphics limited to sparklines and thin rules. Fonts are CSS tokens.

## Consequences
- The public edition is a build flag, not a rewrite. Hosting it is a separate, approved step.
- The console needs the local server for votes and Ask; opened as plain files it is read-only.
- Ask with the local model takes about 20 s and can overstate; switching `reasoning` to Claude needs an API key
  and cost approval (estimated $8-33/month at 10 questions a day depending on the model).
- A tool-calling Ask (the MCP tools behind the page) is the next step once a capable model is configured.
