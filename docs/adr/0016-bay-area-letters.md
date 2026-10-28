# ADR-0016: Add Bay Area Letters, email only, private only

**Status:** Accepted (2026-10-09)

## Context
Bay Area Letters (AI Secret Media Group, sent through Wirely from a Ghost site) is a weekday digest: 12 to 15
stories per issue, each a headline plus a two-sentence "TL;DR" written by the newsletter. It arrives in the Gmail
inbox, not the `Newsletters` label. Evaluated 2026-10-09 against our corpus:

- Overlap: of 24 stories in the 24 and 29 September issues, about 5 were already in the corpus within three days
  (Opus 5.5, AMD and World Labs, Sonnet 5.5, Nvidia buyback, Starship). About 80% is new, mostly funding rounds,
  deals and infrastructure: the deal flow TL;DR and AlphaSignal cover thinly.
- Lens fit: mixed. Agent security and identity, enterprise AI funding and AI-safety hearings fit; space, biotech,
  drones and consumer hardware do not. Relevance scoring sorts this.
- Links: every link, including "View in browser", is a per-subscriber redirect
  (`bayarealetters.com/r/<id>?m=<subscriber uuid>`). Following one registers a click under the reader's identity.
- Terms: the site publishes no terms of use, privacy or legal page (checked 2026-10-09). Default copyright applies.

## Decision
1. Ingest from the email only (`parser: bayarealetters`): a story is an `<h3>` followed by a "TL;DR:" paragraph,
   which excludes promotional blocks, previews of older issues and Ghost sponsor cards. No link is ever followed;
   a test fails on any network call during parsing.
2. Items link to the issue in the owner's Gmail (`#search/rfc822msgid:<Message-ID>`). The corpus never uses that
   URL as a merge key (stories of one issue share it), like AlphaSignal's issue page (ADR-0015).
3. Read from `[Gmail]/All Mail` with a server-side IMAP `FROM` search, so no Gmail filter is needed and the large
   folder is not downloaded. The search now applies to every email source (`senders` on `list_messages`).
4. Private only: `bayarealetters` is in `exclude_sources`, so the public edition, the share file and the public
   brief never contain its items or its mentions. Ask for permission before any public use.
5. History: all 67 issues since 2026-07-06 ingested from the mailbox (886 stories), so trend baselines include it.

## Consequences
- About 65 more items a week, extracted locally at $0; a new independent source family for confidence counts.
- No article URLs: the same story in TL;DR and Bay Area Letters is not merged and counts as two items with
  different families. A title-similarity merge (TODO T26) would fix this for AlphaSignal and Bay Area Letters.
- Console readers click through to Gmail, not the article; the Gmail link works only for the mailbox owner.
- If the publisher changes its template, issues are skipped with a reason in the ingest report.
