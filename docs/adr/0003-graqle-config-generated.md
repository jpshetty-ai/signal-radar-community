# ADR-0003: graqle.yaml is generated from models.yaml

**Status:** Accepted

## Context
Graqle (0.85) has its own model config. Two hand-edited files will drift. Graqle's CLI also falls back to a **mock
backend with only a console warning** when its backend is misconfigured, so drift can produce fake reasoning output
silently. Graqle's chat backends: `anthropic`, `openai`, `bedrock`, `ollama`. Its embedding backends: `local`
(sentence-transformers), `bedrock`, `simple`. No Ollama embeddings.

## Decision
`scripts/sync_graqle_config.py` generates `graqle.yaml`:
- `reasoning` role -> Graqle `model` (used by `graq reason` and `graq mcp serve`)
- every chat role -> a named profile under `models:`
- graph stored at `data/graqle/graqle.json`, dedicated to this project, git-ignored
- `cost.fallback_to_api: false` so Graqle never silently switches to a paid API
- `--check` mode fails when the committed file is stale; a test enforces it
- the generated YAML is validated against Graqle's own `GraqleConfig` pydantic model in tests

## Consequences
- **Known divergence:** Graqle's retrieval embeddings (all-MiniLM-L6-v2, 384 dim) differ from our embedding role
  (nomic-embed-text, 768 dim). The script prints this warning on every run. Vectors from the two must never be mixed.
- Graqle version pinned to `>=0.85,<0.86`; its config schema changes between minors.
- Before relying on any Graqle output, run `graq doctor` and check the backend line is not "mock".
