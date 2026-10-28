# Hosting the public edition (Cloudflare Pages + Access)

One-time setup, about 10 minutes. Background and alternatives: [ADR-0014](adr/0014-hosting-public-edition.md).
Cost: $0/month (Pages free plan, Zero Trust free plan up to 50 users).

## 1. Cloudflare account and Zero Trust (dashboard, once)
1. Create a free account at https://dash.cloudflare.com/sign-up (or sign in).
2. Open **Zero Trust** in the left menu. Choose a team name (it becomes `<team>.cloudflareaccess.com`, the
   login page your viewers see) and select the **Free** plan. Cloudflare may ask for a payment method even
   for the free plan; nothing is charged below 50 users.
3. Copy the **Account ID** (Account home, right column, or the URL after `dash.cloudflare.com/`).

## 2. API token (dashboard, once)
My Profile > API Tokens > Create Token > Custom token, with these account permissions for your account only:
- Account > Cloudflare Pages > Edit
- Account > Access: Apps and Policies > Edit
- Account > Access: Organizations, Identity Providers, and Groups > Edit

Then store it (the token never goes into Git):

```bash
echo 'CLOUDFLARE_ACCOUNT_ID=<account id>' >> .env
security add-generic-password -s signal-radar-cloudflare -a <account id> -w   # paste the token when asked
```

## 3. Viewers
`config/viewers.txt`, one email per line (`#` comments allowed). The file is Git-ignored.

## 4. Create the project and access policy, then publish

```bash
signal-radar deploy --setup      # Pages project, one-time PIN login, viewer policy, Access app (idempotent)
signal-radar deploy --dry-run    # build + checks + summary, uploads nothing
signal-radar deploy              # build, check access, upload, probe the live URL anonymously
```

`deploy` refuses to upload if the Access app does not cover both `<project>.pages.dev` and
`*.<project>.pages.dev`, and fails loudly if the live URL answers without a login.

## 5. Verify (every first deploy, and after changing access)
1. Wait 2-5 minutes.
2. Open the URL in a private browser window: you must see the Cloudflare Access login, not the site.
3. Enter a listed email, enter the PIN from the mail: the site loads, with the publisher footer.
4. Try an email that is not listed: no PIN arrives / access denied.

## 6. Daily refresh
```bash
signal-radar schedule install daily --at 07:30   # launchd job on this Mac: 07:30 and at login, skips if run < 20 h ago
# set `daily: true` in config/publish.yaml once step 5 passed
signal-radar schedule status
signal-radar schedule run daily                  # run it now; log in data/logs/daily.log
```

## Adding or removing a viewer
Edit `config/viewers.txt`, then `signal-radar deploy --setup`. To end someone's session immediately: Zero Trust >
Access > Applications > Signal Radar > revoke, or Zero Trust > My Team > Users > revoke.
