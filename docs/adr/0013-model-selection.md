# ADR-0013: Model selection by measured quality and cost

**Status:** Accepted (2026-10-04)

## Context
Theme cards, Ask and the coming weekly brief need a better writer than the local qwen3:8b, which overstates and
forces the reader's angle. The Anthropic API would cost about $12/month at our volume with Opus 5.5 (above the
$10/month approval threshold). Open-weight models now score close to the frontier on public benchmarks at a
fraction of the price (Oct 2026: MiMo-V2.6-Pro $0.435/$0.87 per 1M tokens, GLM-5.3 $1.40/$4.40, Sonnet 5.5
$2/$10), but benchmarks measure reasoning and coding, not faithful summarisation with citations.

## Decision
- **Providers are named profiles of a kind** (`ollama`, `anthropic`, `openai` = any OpenAI-compatible API).
  OpenRouter is configured as one profile: one key for every model, pay as you go. Its routing is restricted
  to providers that do not collect data (`data_collection: deny`), offer zero data retention (`zdr: true`) and
  support our structured output (`require_parameters: true`). OpenRouter has no region pinning; data may be
  processed outside the EU by no-retention providers.
- **A model catalog** in models.yaml names each choice with provider, model, params, price and data policy.
  No model name appears in code.
- **Roles take ranked candidates** with a strategy: `first_available` or `cheapest_passing` (cheapest candidate
  whose last measured quality for that role meets `min_quality`). The last candidate is always local, so the
  pipeline works without keys, network or budget. The router falls through candidates on any failure and
  records which one served each call.
- **Budget in code:** `budget.monthly_usd` (5). When this month's priced spend reaches it, paid candidates are
  skipped. Raising it above 10 needs the user's approval.
- **Evaluation on our own tasks** (`signal-radar models eval`): real theme-writing and Ask tasks from the signal
  store, the same prompts as production. Automatic checks (valid output, names and numbers grounded in the
  evidence, citations real and sufficient, house style) plus an optional blind judge model rating faithfulness,
  usefulness and writing. The run prints a cost estimate, refuses above `--max-usd`, asks for confirmation, and
  writes a side-by-side report (data/model_eval/*.md) and the scores the strategy reads (data/model_eval.json).
- **Three kinds of judge**, all applicable to a saved run afterwards (`models judge`), with agreement between
  judges reported (Spearman rank correlation, mean difference):
  - a catalog model grading blind on a 1-5 rubric;
  - **Jev** (TypeSafe System One, `judges:` in models.yaml): per-sentence Choice (supported / overstated /
    contradicted / unsupported / interpretation) against the evidence, a usefulness Score and a forced-angle
    Noul, in one request per output. $0.042 per 1M input tokens, output free: a judged run costs cents;
  - **blind grading** by a person, or by Claude in a session under the user's subscription (`models blind`
    writes a sheet with anonymous labels and a separate key; `models grades` imports). Blind grades become the
    primary judge when present.
- **Generated prose is cached per preferred candidate** and stored only when that candidate wrote it, so a
  model change regenerates text and a fallback answer is never frozen.

## Consequences
- Choosing a model is a measured decision that can be repeated whenever prices or models change.
- The automatic checks alone rate the local model about 0.95: they cannot see overstatement or weak judgement.
  Quality bars are only meaningful with the judge (or a human reading the report).
- A judge that is also a candidate may prefer its own outputs; the report flags this.
- Jev is a decision model, not a reasoner: it needs validating on our data. The plan is to grade one run blind
  in-session and compare with Jev; if they agree, Jev becomes the routine judge.
- One more account and key (OpenRouter), stored in .env or the macOS keychain.

## Result of the first judged run (2026-10-05, run 20261005T091511)

9 candidates (Opus left out: too expensive to be the daily writer), 14 tasks from our own data, $0.62 actual
against a $0.43 estimate (Kimi K3 and Gemini 3.8 Flash billed about twice their estimate, likely hidden reasoning
tokens), plus $0.015 for Jev. Claude graded all 120 outputs blind in-session; blind grades are the primary judge.

| Role | Blind-graded quality (top) | Chosen | Why |
|---|---|---|---|
| writing | sonnet-5.5 0.95, gpt-6-luna 0.92, mimo 0.86, local 0.74 | **gpt-6-luna** | Ties Sonnet on the blind grade (0.90) at about 1/23 of the cost ($0.0003 per task) |
| reasoning | sonnet-5.5 0.95, gpt-6-luna 0.94, mimo/glm-5.3 0.91, local 0.67 | **gpt-6-luna** | Within 0.01 of Sonnet at about 1/24 of the cost |

- Candidates are now gpt-6-luna, sonnet-5.5 (quality fallback), mimo-v2.6-pro, local; `cheapest_passing` picks
  gpt-6-luna. glm-5.3 dropped (3 of 6 writing tasks returned invalid JSON); deepseek-v4-pro scored lower on both.
- The local model fails the 0.85 bar once judged (0.74 writing, 0.67 reasoning): its main faults were vague
  "why it matters" lines and lens claims the items do not support.
- Common faults across models: stating an opinion piece's argument as fact, invented cause and effect, picking one
  of two conflicting figures, and forcing an EU AI Act angle onto items that do not support it.
- **Jev does not agree with the blind grades** (Spearman 0.115 over 120 outputs): it does not catch these faults
  reliably, so it is not adopted as the routine judge. Keep blind grading for future model changes.
- Caveats: small samples (6 writing, 8 reasoning tasks); the blind grader is a Claude model and Sonnet 5.5 is a
  candidate, so some self-preference is possible, though the chosen model is not Anthropic's.
- Expected running cost with gpt-6-luna: well under $1/month (daily theme cards, weekly brief, Ask).
