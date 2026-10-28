# ADR-0007: Microsoft 365 mail via Graph, device code sign-in

**Status:** Accepted (amends ADR-0004); newsletter mailbox moved to Gmail by ADR-0008, code kept

## Context
TL;DR is delivered to a Microsoft 365 business mailbox (EU tenant, federated sign-in).
Exchange Online has switched off basic (password) auth for IMAP, so the planned "IMAP + keychain password"
adapter cannot work. Checking a real issue (2026-10-02) also showed that every TL;DR edition comes from the same
address (`dan@tldrnewsletter.com`) with headline-only subjects. Editions differ only by sender display name
("TLDR AI", "TLDR", "TLDR Founders") and the `<h1>` "TLDR AI 2026-10-02" in the body.

Options considered: Graph API with delegated `Mail.Read` (chosen); IMAP with XOAUTH2 (same app registration,
more moving parts, needs IMAP left enabled); forwarding to Gmail (Exchange Online blocks external auto-forwarding
by default); the Claude Microsoft 365 connector (works only inside a Claude session, no unattended ingest).

## Decision
- `imap_accounts` becomes `mail_accounts`, a union on `kind`: `graph` (implemented) or `imap` (schema only).
- A `graph` account names env vars for tenant id, client id and user. The Entra app "Signal Radar" is a public
  client (no secret), delegated `Mail.Read` only.
- Sign-in is MSAL's device code flow, run once by a human: `signal-radar auth work`. The token cache is stored in
  the macOS keychain (`msal-extensions` `KeychainPersistence`, service `signal-radar-m365`), never on disk in
  clear. Unattended runs use the refresh token silently. When it expires, tools fail with a message saying to
  sign in again.
- Least privilege in practice: delegated `Mail.Read` covers the whole mailbox and cannot be scoped to a folder.
  So an Outlook rule moves TL;DR into its own folder, the adapter reads only the configured folder, and it never
  marks read, moves or deletes.
- `EmailMatch` gains `from_name`: exact, case-insensitive display name match. It is exact because "TLDR" is a
  substring of every edition name.
- The TL;DR parser unwraps `tracking.tldrnewsletter.com/CL0/<encoded-url>/...` links locally and strips `utm_*`.
  We never request the tracking URL: that would count as a click and attach your identity to it.
- ADR-0004's `suggest_newsletter_filter(sample_message_id)` became `suggest_newsletter_filter(account, folder,
  days)`: it groups senders and proposes a match block, which is more useful when one address serves many editions.

## Consequences
- One-time Entra app registration per tenant. Reseller-managed tenants may restrict this.
- Refresh tokens last 90 days (sliding) unless tenant policy says otherwise. A daily job keeps it alive; a long
  break needs another `signal-radar auth work`.
- The keychain prompt may appear the first time a new binary (e.g. launchd job) reads the token.
- Real email samples for parser regression tests live in `data/samples/` (git-ignored) because their links carry
  personal unsubscribe tokens. The committed fixture is synthetic.
- `links.tldrnewsletter.com/<code>` short links are kept as-is. Resolving them needs a network call per item, and
  dedupe across editions misses them for now.
