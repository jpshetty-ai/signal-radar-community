# ADR-0002: LLM router

**Status:** Accepted

## Context
Models must be swappable per role without code changes, and compared on cost and quality over the same inputs.
Providers in scope: Ollama (local), Anthropic, OpenAI and OpenAI-compatible servers.

## Options considered
1. **LiteLLM**: one call shape for 100+ providers. Large dependency surface, frequent releases, and it sits in the
   credential path for every call. Abstracts away provider features we want (Anthropic effort, server-side fallbacks,
   Ollama `think`/`format`).
2. **Own thin router** (chosen): one adapter class per provider, about 60 lines each, behind a `ProviderClient` protocol.

## Decision
`src/signal_radar/llm.py` is the only module that imports a provider SDK. Callers ask for a role:
`router.complete("extraction", prompt, json_schema=...)`. Role -> provider/model/params comes from
`config/models.yaml`; `--model role=provider/model` overrides per run. SDKs are imported lazily and are optional extras.

Details:
- `params` are passed through per provider, so provider-specific knobs stay in config, not code.
  An override that changes provider drops the role's params, since they would not apply.
- Structured output uses each provider's native schema mode (Ollama `format`, Anthropic `output_config.format`,
  OpenAI `response_format`), and the router parses JSON into `.data`.
- Anthropic: `stop_reason == "refusal"` raises `LLMRefusal`. `fallbacks: default` in params enables the server-side
  refusal fallback (beta `server-side-fallback-2026-07-01`); the call is priced by `response.model`, the model that
  actually served it.
- Embeddings are a role like any other; Anthropic has no embedding endpoint, so that combination fails loudly.

## Consequences
- Adding a provider means one class plus a `Provider` literal in `config.py`.
- No streaming yet. Fine for batch jobs with `max_tokens` up to 16k; long-form writing beyond that needs a streaming path.
- No retry layer of our own: Anthropic/OpenAI SDKs retry 429/5xx; Ollama is local.
