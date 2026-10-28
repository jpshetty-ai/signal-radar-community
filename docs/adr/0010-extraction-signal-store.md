# ADR-0010: Local-model extraction, deterministic signal store, Graqle push

**Status:** Accepted (2026-10-03, decided autonomously while the user was away; revisit)

## Context
Phase 4 needs entities, a market-map category and a relevance score per item, then trend maths over time.
No Anthropic key is configured, and extraction runs over every item, so it must be cheap to re-run.

## Decision
- **Extraction** uses the `extraction` role (local Ollama `qwen3:8b`, free). One call per item with a JSON schema
  built from `config/taxonomy.yaml` (categories, reader lens, relevance scale, ontology). The schema asks for the
  category before the entities. The raw JSON is stored per item in `data/signals.sqlite:extractions` with model and
  an input hash (taxonomy + prompt version + item text), so runs are resumable and stale items are detectable.
- **Signal store** tables (items, entities, mentions, relations) are **rebuilt** from corpus + stored extractions
  with no LLM calls (`signal-radar signals rebuild`, about a second). The rebuild also enforces quality rules:
  canonical keys + `config/aliases.yaml`; generic entities dropped ("AI", "LLM", ...); entities not named in the
  item text dropped (the model copied the reader's interests into entity lists during testing); relations
  checked against type rules and flipped when merely reversed.
- **Metrics**: current window vs the mean of the previous 4 windows of equal size. Mover score = (cur - base) /
  sqrt(base + 1). Confidence: high (>= 8 mentions and both source families), medium (>= 4), low otherwise.
  Fading = the same score, negative. Clusters = entity pairs co-mentioned more often than in the baseline.
  Reading list = relevance + corroboration across sources + entity momentum + (AlphaSignal) engagement,
  with each component visible.
- **Graqle**: `graq scan docs corpus` (document nodes keyed by item id) + `graq learn batch` (entity nodes
  `ent:<key>`, MENTIONS edges, validated relations), both run from `data/graqle/`.

## Consequences
- About 11 s per item on this Mac: the first full run over 2,759 items takes about 8.5 hours (newest first).
  Daily increments (about 40 items) take a few minutes.
- qwen3:8b quality is adequate for categories and entity names. Its relations are weak; most are filtered out.
  The relevance score is a first heuristic, to be replaced by the user's feedback from the website.
- Changing the taxonomy or the prompt makes the stored extractions stale; `signal-radar extract` re-runs them.
- Swapping in a stronger model is one line in models.yaml or `--model extraction=...`, and the call log compares models on identical inputs.
