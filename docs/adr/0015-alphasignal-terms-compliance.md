# ADR-0015: AlphaSignal from the email only; left out of the public edition entirely

**Status:** Accepted (2026-10-05)

## Context
AlphaSignal's terms (alphasignal.ai/terms) allow personal, non-commercial use of its content only (§7.2) and forbid
automated access to or scraping of its site (§6). Until now the parser (ADR-0008) fetched each issue's public web
version (`alphasignal.ai/email/<issue id>`, one anonymous GET per issue) to recover the real article URLs, because
every link in the email is a per-subscriber redirect we must not resolve. That fetch is automated access. The
public edition (ADR-0014), shown to invited readers under the publisher's name, also counted AlphaSignal items:
367 of 2,820 items and 1,406 of 10,740 entity mentions, plus co-coverage of 69 TL;DR items.

## Decision
1. **No requests to AlphaSignal's site.** The parser reads the email only: titles, sections, engagement counts and
   summaries all come from the email. Each item links to its issue's public page, built from the issue id in the
   email and never requested. A test fails if parsing touches the network.
2. **History kept as is.** The 367 items stored before 2026-10-05 keep the article URLs resolved then; the corpus
   is the private record and is not rewritten.
3. **Separate public store.** The public edition and the single-file share are built from
   `data/signals_public.sqlite`, rebuilt from the corpus with `exclude_sources` (from `config/publish.yaml`,
   default `[alphasignal]`) left out as if never ingested: items whose source is excluded are dropped, and
   excluded entries are removed from other items' `also_in`. Every count, theme, entity page and brief signal is
   therefore computed without AlphaSignal, not filtered afterwards. Extractions and cached prose are copied from
   the main store (no extraction calls); votes are not copied.
4. **Own public brief.** `signal-radar brief` writes the console brief and a public brief
   (`data/briefs/<week>.public.json`) computed from the public store, with its own prose call, because the console
   prose is written over AlphaSignal summaries. The console brief is never served publicly.
5. **Deploy guard.** `check_bundle` refuses any page or data file that names an excluded source, so a build from
   the wrong store cannot be uploaded. The public Method page says one personal-use newsletter is left out,
   without naming it.

## Consequences
- Compliant with §6 and §7.2 as we read them, without losing AlphaSignal for the private console.
- New AlphaSignal items link to the issue page, not the article: one click further for the reader, and the
  cross-source URL merge no longer finds the same story in TL;DR, so new AlphaSignal items stop adding co-coverage
  ("covered by AlphaSignal + TLDR") in the console. A title-similarity merge could restore this later.
- Stories in one issue share a URL; the corpus treats `alphasignal.ai/email/` like TL;DR short links (never a
  merge key), and the item id (URL + title) keeps them apart.
- The public edition is smaller (2,453 items, 472 entities) and some signals change rank: GPU and Codex were
  "rising" only with AlphaSignal's mentions.
- A weekly brief now costs two `writing` calls instead of one.
- Permission requests to AlphaSignal and TL;DR are drafted separately; if AlphaSignal grants public use, remove it
  from `exclude_sources`.
- Not legal advice: this is our reading of the terms on 2026-10-05.
