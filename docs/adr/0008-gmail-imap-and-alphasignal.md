# ADR-0008: Gmail over IMAP as the newsletter mailbox; AlphaSignal via its public web version

**Status:** Accepted (newsletter mailbox moves from M365, ADR-0007, to Gmail). The AlphaSignal web-version fetch is superseded by ADR-0015: email only.

## Context
Newsletters are consolidated in personal Gmail: AlphaSignal already arrived there, and
TL;DR was re-subscribed there on 2026-10-03. A Gmail filter labels both senders "Newsletters".

Gmail options compared: IMAP with an app password (chosen); the Gmail API with `gmail.readonly` (narrower, but for a
personal account the OAuth app is "External". In Testing status its refresh tokens expire after 7 days, which
breaks a daily job, and publishing a restricted-scope app may need Google's verification); a dedicated newsletter
account (smallest blast radius, but a third re-subscription).

AlphaSignal: every link in the email is an opaque per-subscriber redirect (`app.alphasignal.ai/c?uid=...&lid=...`).
The real destination is not in the email. Requesting the redirect would register a click under the subscriber's
identity.

## Decision
- `mail_accounts.gmail`: `kind: imap`, host `imap.gmail.com`, user from `IMAP_GMAIL_USER`, password = a Google app
  password in the macOS keychain (service `signal-radar-imap`, account = the address). No password in env or files.
- The IMAP reader is read-only by construction: `EXAMINE` (`select(readonly=True)`) and `BODY.PEEK[]`, so the
  server refuses changes and newsletters stay unread. It reads only the configured label.
- TL;DR sources point at `gmail:Newsletters`. The M365 account and its code stay (tested, documented) but no source
  uses them.
- AlphaSignal parser (`parser: alphasignal`):
  - structure (sections, engagement counts, sponsor markers) is read from the email
  - real URLs come from the public web version `alphasignal.ai/email/<issue id>`, the link AlphaSignal provides for
    forwarding: one anonymous GET per issue, no user token. The two renderings must list the same stories, else
    the issue is deferred
  - if the web version can't be fetched or doesn't match, the issue is **deferred** (retried next run), never
    stored with tracker URLs: a later correct parse would produce different item ids and duplicates
  - sponsors are dropped by their most stable marker: no engagement count (plus "Presented by", ad-server hosts)
  - "Sunday Deep Dive" issues are skipped (single essays without engagement data, sometimes partner topics)
  - engagement ("16,862 Likes") is stored raw in `Item.engagement`. What it counts is not documented by
    AlphaSignal, so it is an attention hint, not a measured metric
- Parsers can raise `SkipMessage` (counted in `FetchReport.skipped`) or `ParseDeferred` (counted in
  `FetchReport.deferred`, with a warning).

## Consequences
- An app password grants full IMAP access (read and write) to the whole mailbox. Read-only use is enforced in our
  code, not by Google. Revoke at myaccount.google.com/apppasswords.
- macOS may ask once to let Python read the keychain item; "Always Allow" is needed for unattended runs.
- AlphaSignal ingest depends on alphasignal.ai's web version layout. A layout change shows up as deferred issues
  with a warning, not as silent bad data.
- AlphaSignal story bodies are capped at 600 characters (they are longer than TL;DR summaries).
