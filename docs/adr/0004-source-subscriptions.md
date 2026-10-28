# ADR-0004: Source subscriptions ("RSVP" to feeds and newsletters)

**Status:** Accepted (mail account and filter details amended by ADR-0007)

## Context
New sources must be addable without code: RSS/Atom feeds, Hacker News queries, arXiv queries, GitHub releases,
and email newsletters beyond TL;DR.

## Decision
`config/sources.yaml` is the subscription registry, validated by a pydantic discriminated union on `type`.

- **Feed-type sources** (`rss`, `hn`, `arxiv`, `github_releases`): subscribing is adding an entry. The adapter polls it.
- **Email newsletters** (`email_newsletter`): Signal Radar cannot sign up on a publisher's site for you (double
  opt-in, CAPTCHAs). You subscribe once with an address the IMAP account reads, ideally a plus-alias such as
  `you+radar@gmail.com` filtered into a label. The entry then tells the adapter which messages belong to the source
  (`from` substring, `subject_contains`, `subject_excludes`) and which parser to use (`tldr`, `generic`).
- Credentials are never in this file: IMAP accounts reference env var names and an OS keychain service.

Phase 2 MCP tools on top of this:
- `list_sources()`
- `subscribe_source(source: SourceSpec)`: validates and appends; for `rss`, fetches the feed once to prove it parses.
- `unsubscribe_source(source_id, purge=False)`: disables the entry (or removes it); corpus items stay.
- `suggest_newsletter_filter(sample_message_id)`: proposes a `match` block from a real email in the mailbox.
- `import_opml(path)`: bulk-subscribe feeds from another reader.

## Consequences
- `save_sources` rewrites the YAML and drops comments. Acceptable: the file is small and Git shows every change.
- Unsubscribing from the newsletter itself (the publisher's list) stays manual.
