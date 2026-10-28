# ADR-0006: LLM call log in SQLite

**Status:** Accepted

## Decision
Every call (success or failure) writes a row to `data/llm_calls.sqlite`: run id, job, role, provider, requested and
served model, input hash, tokens, cost, latency, stop reason, error.

`input_hash` is SHA-256 over (system, messages, schema), excluding the model, so the same prompt sent to two models
shares a hash. `signal-radar compare` groups by model over hashes seen by more than one model.

Cost comes from the `pricing` table in `models.yaml`. A model without a price is logged with `cost_usd = NULL`
and counted as `unpriced_calls` in run summaries. Prices are never guessed.

## Why SQLite over JSONL
The main question is a GROUP BY across runs. SQLite answers it in one query and is already the signal store's engine.
