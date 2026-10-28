# ADR-0014: Host the public edition on Cloudflare Pages behind Cloudflare Access; run jobs on the Mac

**Status:** Accepted (2026-10-04)

## Context
The public edition (ADR-0011) should be readable by a few named people under NDA through a link, refreshed
daily, at close to zero cost and with no public listing. Rules: anything above $10/month needs approval; access
is limited to named email addresses; only `data/site_public/` may leave the machine (never the console edition,
the corpus, `data/*.sqlite`, `.env` or newsletter summaries); secrets live in the keychain or `.env`.

Options compared on 2026-10-04 (prices from vendor pages and recent reviews):

| Option | Per-email access | EU hosting | Cost/month | Verdict |
|---|---|---|---|---|
| Cloudflare Pages + Access (one-time PIN) | Yes, free up to 50 users | No (global edge; EU-only data is Enterprise) | $0 | **Chosen** |
| GitHub Pages | Only with Enterprise Cloud (~$21/user) | No | $0 public / $21+ private | Fails access rule |
| S3 + CloudFront + Cognito/Lambda@Edge | Yes, but we build the login | Origin in EU | ~$0-2 | Most moving parts |
| Netlify | Per-viewer only on Pro ($19); free = one shared password | No | $19 | Cost, shared password |
| Hetzner VPS + Caddy + oauth2-proxy | Yes, via Google/Microsoft login | Yes | ~€4.49 (CX23) | Fallback if EU hosting becomes a requirement |
| Bunny.net / Scaleway buckets | Signed links only (forwardable) | Yes | ~$1 | Fails access rule |

Where the jobs run: the pipeline needs the local Ollama model and the keychain (Gmail app password). Running it in
GitHub Actions or on a VPS would move mail credentials and newsletter text off the Mac and swap the local
extraction model for a paid one (about $5-10/month at 40 items/day on a small cloud model).

## Decision
- **Hosting:** Cloudflare Pages, uploaded with `wrangler pages deploy` (direct upload, no Git integration, so the
  repository and corpus are never connected to Cloudflare). Free `*.pages.dev` hostname for now.
- **Access control:** one self-hosted Cloudflare Access application covering **both** `<project>.pages.dev` and
  `*.<project>.pages.dev`. The Pages "enable access policy" switch only protects preview URLs, and every
  deployment keeps its own hash URL forever, so the wildcard is required. Login is the email one-time PIN; the
  only policy is an allow-list of emails from `config/viewers.txt` (Git-ignored, personal data). Session: 7 days.
- **`signal-radar deploy`** builds the public edition and runs three guards: (1) bundle allowlist of paths, no
  `summary`/`vote` fields, no console script, Pages size limits; (2) refuse to upload unless the Access app covers
  both hostnames with email-only allow policies; (3) after upload, anonymous requests to the production and
  deployment URLs must be redirected to the Access login, otherwise the run fails with "PUBLISHED BUT NOT
  PROTECTED". `--dry-run` prints what would be published; `--setup` creates the project, one-time PIN login,
  policy and Access app through the API (idempotent; re-run after editing the viewer list).
- **The site is a snapshot.** It never calls back to the Mac or the graph; it reads the JSON exported at build
  time. No inbound connection, tunnel or cloud database.
- **Jobs run on the Mac** with launchd (`signal-radar schedule install daily`), as a LaunchAgent in the user
  session so the keychain and Ollama are reachable. A run missed during sleep runs at the next wake. Publishing
  from `daily` is opt-in (`daily: true` in `config/publish.yaml`).
- **Publisher line:** each public page carries a footer from `config/publish.yaml`: publisher name, invited-readers
  notice, automated-signals caveat, original content belongs to its publishers, not advice. Pages are
  `noindex` (meta tag and `X-Robots-Tag` header) and send `Referrer-Policy: same-origin`, so publishers do not
  see the private URL.
- **Secrets:** Cloudflare API token in the macOS keychain (service `signal-radar-cloudflare`, account = account
  id), `CLOUDFLARE_API_TOKEN` in `.env` as fallback; account id in `.env`. Token scope: Pages edit and Access
  apps/policies edit for this account only.

## Consequences
- $0/month. Cloudflare (US company, EU-US Data Privacy Framework) processes viewers' email addresses and serves
  the processed public data from its global edge. Not EU-only hosting; revisit (Hetzner row above) if a client
  or partner requires it.
- Daily freshness depends on the Mac being awake once a day. "Runs while the laptop is closed" needs option C in
  the comparison (always-on VPS) and a paid extraction model: a separate, approved decision.
- Viewers cannot ask questions (no live model in the public edition). A hosted Ask would need a cloud model and a
  copy of the signal store, deferred.
- Revoking a viewer: remove the line from `config/viewers.txt`, run `signal-radar deploy --setup`. Existing
  sessions last until they expire (up to 7 days) unless revoked in the Zero Trust dashboard.
- Legal text is a sensible default, not legal advice; terms of use, a privacy notice for viewers and the
  copyright position of headlines are open items before any wider audience.
