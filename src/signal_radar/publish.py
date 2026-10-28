"""Publish the public edition to Cloudflare Pages, behind Cloudflare Access (ADR-0014).

Only data/site_public/ leaves the machine. Three guards run before every upload:
1. Bundle check: an allowlist of paths and no newsletter text (no `summary` or `vote` field anywhere), so a
   console build or a stray file can never be published by mistake.
2. Access check: an Access application must cover both the production hostname and every per-deployment
   hostname (`*.<project>.pages.dev`), with allow policies that name email addresses only.
3. Probe after upload: an anonymous request to the live URLs must be redirected to the Access login. A 200
   means the site is public, and the run fails loudly.

`setup()` creates what the checks expect (Pages project, one-time PIN login, viewer policy, Access app) through
the Cloudflare API, so the dashboard is only needed once, to turn on Zero Trust.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from .config import CONFIG_DIR, DATA_DIR, PROJECT_ROOT

API = "https://api.cloudflare.com/client/v4"
PUBLIC_DIR = DATA_DIR / "site_public"
APP_NAME = "Signal Radar"
POLICY_NAME = "Signal Radar viewers"
DEFAULT_EXCLUDED = ["alphasignal"]   # personal, non-commercial use only (ADR-0015)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CloudflareSettings(_Strict):
    project: str
    production_branch: str = "main"
    account_id_env: str = "CLOUDFLARE_ACCOUNT_ID"
    token_keyring_service: str | None = "signal-radar-cloudflare"
    token_env: str = "CLOUDFLARE_API_TOKEN"
    viewers_file: str = "config/viewers.txt"
    session_duration: str = "168h"
    wrangler: list[str] = ["npx", "--yes", "wrangler@4"]


class PublishConfig(_Strict):
    publisher: str
    notice: str
    daily: bool = False
    # Sources whose terms allow personal use only: left out of the public edition entirely (ADR-0015)
    exclude_sources: list[str] = DEFAULT_EXCLUDED
    # Public edition only, each shown only when set: Cloudflare Web Analytics beacon token (cookieless; public by
    # design, it sits in the page source), a feedback form link in the footer, and the privacy contact.
    web_analytics_token: str | None = None
    web_analytics_pages: bool = False   # turned on in the Pages project instead: Cloudflare adds the beacon itself
    feedback_url: str | None = None
    privacy_contact: str | None = None
    cloudflare: CloudflareSettings

    def footer(self, year: int | None = None) -> str:
        return self.notice.format(year=year or date.today().year, publisher=self.publisher).strip()


def load_publish(path: Path | None = None) -> PublishConfig:
    path = path or CONFIG_DIR / "publish.yaml"
    return PublishConfig.model_validate(yaml.safe_load(path.read_text()) or {})


# ------------------------------------------------------------------ bundle check

ALLOWED = [re.compile(p) for p in (
    r"(index|brief|explore|method)\.html", r"_headers", r"static/(radar\.css|explore\.js)",
    r"data/(snapshot|entities)\.json", r"data/entity/(?!.*\.\.)[a-z0-9.][a-z0-9.-]*\.json")]  # site.data.entity_slug
FORBIDDEN_KEYS = {"summary", "vote", "votes"}   # newsletter text and private feedback
# '"/api/' is a site-relative console route; an article URL that merely contains /api/ is fine
FORBIDDEN_HTML = ("console.js", 'id="ask"', '"/api/', "'/api/", "data-vote")
MAX_FILES, MAX_FILE_BYTES = 20_000, 25 * 1024 * 1024   # Cloudflare Pages limits per deployment


@dataclass
class Bundle:
    root: Path
    files: int = 0
    bytes: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


def check_bundle(root: Path, excluded: list[str] = ()) -> Bundle:
    """Everything that would be uploaded, and every reason not to upload it.

    `excluded`: source ids that must not appear in any page or data file, not even as a label (ADR-0015).
    """
    b = Bundle(root)
    named = re.compile(r"\b(" + "|".join(map(re.escape, excluded)) + r")\b", re.I) if excluded else None
    if not (root / "index.html").is_file():
        b.problems.append(f"{root} has no index.html (build the public edition first)")
        return b
    for p in sorted(x for x in root.rglob("*") if x.is_file() or x.is_symlink()):
        rel = p.relative_to(root).as_posix()
        b.files += 1
        if p.is_symlink():
            b.problems.append(f"symlink not allowed: {rel}")
            continue
        size = p.stat().st_size
        b.bytes += size
        kind = "entity" if rel.startswith("data/entity/") else p.suffix.lstrip(".") or rel
        b.by_kind[kind] = b.by_kind.get(kind, 0) + 1
        if not any(r.fullmatch(rel) for r in ALLOWED):
            b.problems.append(f"unexpected file: {rel}")
        if size > MAX_FILE_BYTES:
            b.problems.append(f"over 25 MiB: {rel}")
        if named and p.suffix in (".json", ".html") and (hit := named.search(p.read_text())):
            b.problems.append(f"{rel} names excluded source {hit.group(0)!r}")
        if p.suffix == ".json":
            try:
                bad = _keys(json.loads(p.read_text())) & FORBIDDEN_KEYS
            except ValueError:
                b.problems.append(f"invalid JSON: {rel}")
                continue
            if bad:
                b.problems.append(f"{rel} has private fields {sorted(bad)} (console data?)")
        elif p.suffix == ".html":
            text = p.read_text()
            hits = [s for s in FORBIDDEN_HTML if s in text]
            if hits:
                b.problems.append(f"{rel} has console features {hits}")
    if b.files > MAX_FILES:
        b.problems.append(f"{b.files} files, over the Pages limit of {MAX_FILES}")
    return b


def plan(bundle: Bundle, cfg: PublishConfig, host: str | None = None) -> dict:
    """The printed summary of what would be published."""
    snap = json.loads((bundle.root / "data" / "snapshot.json").read_text()) if not bundle.problems else {}
    meta = snap.get("meta", {})
    return {"folder": str(bundle.root), "files": bundle.files, "mb": round(bundle.bytes / 1e6, 2),
            "by_kind": bundle.by_kind, "data_to": meta.get("as_of"), "items": meta.get("items"),
            "themes": [t.get("title") for t in snap.get("themes", [])], "project": cfg.cloudflare.project,
            "url": f"https://{host}" if host else f"https://{cfg.cloudflare.project}.pages.dev (or with a suffix)",
            "problems": bundle.problems}


# ------------------------------------------------------------------ credentials and viewers

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def read_viewers(path: Path) -> list[str]:
    """One email per line; '#' starts a comment. Lower-cased and de-duplicated, order kept."""
    if not path.is_file():
        raise PublishError(f"no viewer list at {path}: add one email address per line (the file is Git-ignored)")
    out: list[str] = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip().lower()
        if not line:
            continue
        if not EMAIL.fullmatch(line):
            raise PublishError(f"{path.name} line {n} is not an email address")
        if line not in out:
            out.append(line)
    if not out:
        raise PublishError(f"{path} lists no email addresses")
    return out


def credentials(cfg: PublishConfig) -> tuple[str, str]:
    c = cfg.cloudflare
    account = os.environ.get(c.account_id_env, "").strip()
    if not account:
        raise PublishError(f"set {c.account_id_env} in .env (Cloudflare dashboard > Account home > Account ID)")
    token = None
    if c.token_keyring_service:
        try:
            import keyring

            token = keyring.get_password(c.token_keyring_service, account)
        except Exception:  # noqa: BLE001 - no keychain backend: fall back to the environment
            token = None
    token = token or os.environ.get(c.token_env, "").strip()
    if not token:
        raise PublishError(f"no Cloudflare API token: run `security add-generic-password -s "
                           f"{c.token_keyring_service} -a {account} -w` (or set {c.token_env})")
    return account, token


class PublishError(RuntimeError):
    pass


# ------------------------------------------------------------------ Cloudflare API


class Cloudflare:
    """The handful of Cloudflare API calls this needs. Every response is {success, errors, result}."""

    def __init__(self, account_id: str, token: str, client: httpx.Client | None = None):
        self.a = account_id
        self.http = client or httpx.Client(base_url=API, timeout=30)
        self.http.headers["Authorization"] = f"Bearer {token}"

    def _call(self, method: str, path: str, **kw):
        r = self.http.request(method, f"/accounts/{self.a}{path}", **kw)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 404 and method == "GET":
            return None
        if r.is_error or not body.get("success", False):
            errs = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in body.get("errors", [])) or r.text[:200]
            raise PublishError(f"Cloudflare {method} {path} -> {r.status_code}: {errs}")
        return body["result"]

    def project(self, name: str) -> dict | None:
        return self._call("GET", f"/pages/projects/{name}")

    def create_project(self, name: str, branch: str) -> dict:
        return self._call("POST", "/pages/projects", json={"name": name, "production_branch": branch})

    def identity_providers(self) -> list[dict]:
        return self._call("GET", "/access/identity_providers") or []

    def create_otp(self) -> dict:
        return self._call("POST", "/access/identity_providers",
                          json={"name": "One-time PIN", "type": "onetimepin", "config": {}})

    def policies(self) -> list[dict]:
        return self._call("GET", "/access/policies") or []

    def put_policy(self, body: dict, policy_id: str | None) -> dict:
        if policy_id:
            return self._call("PUT", f"/access/policies/{policy_id}", json=body)
        return self._call("POST", "/access/policies", json=body)

    def apps(self) -> list[dict]:
        return self._call("GET", "/access/apps") or []

    def put_app(self, body: dict, app_id: str | None) -> dict:
        if app_id:
            return self._call("PUT", f"/access/apps/{app_id}", json=body)
        return self._call("POST", "/access/apps", json=body)


def hostnames(project: dict) -> tuple[str, str]:
    """Production hostname and the wildcard that covers every per-deployment URL (they stay live forever)."""
    host = project["subdomain"]
    return host, f"*.{host}"


def _app_hosts(app: dict) -> set[str]:
    hosts = {d.get("uri", "").split("/", 1)[0] for d in app.get("destinations") or [] if d.get("type") == "public"}
    if app.get("domain"):
        hosts.add(app["domain"].split("/", 1)[0])
    return hosts


def _policy_emails(policy: dict) -> set[str] | None:
    """Emails an allow policy admits, or None if it admits anyone by another rule (domain, everyone, ...)."""
    if policy.get("decision") != "allow":
        return None
    emails = set()
    for rule in policy.get("include") or []:
        if set(rule) != {"email"}:
            return None
        emails.add(rule["email"]["email"].lower())
    return emails


def setup(cfg: PublishConfig, cf: Cloudflare, viewers: list[str], log=lambda m: None) -> dict:
    """Create or update the Pages project, one-time PIN login, viewer policy and Access app. Idempotent."""
    c = cfg.cloudflare
    project = cf.project(c.project)
    if project is None:
        project = cf.create_project(c.project, c.production_branch)
        log(f"created Pages project {c.project}")
    host, wildcard = hostnames(project)

    otp = next((p for p in cf.identity_providers() if p.get("type") == "onetimepin"), None)
    if otp is None:
        otp = cf.create_otp()
        log("enabled one-time PIN login")

    existing = next((p for p in cf.policies() if p.get("name") == POLICY_NAME), None)
    policy = cf.put_policy({"name": POLICY_NAME, "decision": "allow", "session_duration": c.session_duration,
                            "include": [{"email": {"email": e}} for e in viewers]},
                           existing and existing["id"])
    log(f"{'updated' if existing else 'created'} viewer policy ({len(viewers)} addresses)")

    app = next((a for a in cf.apps() if a.get("name") == APP_NAME or host in _app_hosts(a)), None)
    body = {"name": APP_NAME, "type": "self_hosted", "domain": host,
            "destinations": [{"type": "public", "uri": host}, {"type": "public", "uri": wildcard}],
            "policies": [{"id": policy["id"], "precedence": 1}], "allowed_idps": [otp["id"]],
            "auto_redirect_to_identity": True, "session_duration": c.session_duration,
            "app_launcher_visible": False}
    app = cf.put_app(body, app and app["id"])
    log(f"Access app covers {host} and {wildcard}")
    return {"url": f"https://{host}", "viewers": len(viewers), "access_app": app.get("id")}


def verify_access(cf: Cloudflare, project: dict, viewers: list[str] | None = None) -> dict:
    """Refuse-to-publish check: both hostnames covered, and only by email allow-lists."""
    host, wildcard = hostnames(project)
    policies = {p["id"]: p for p in cf.policies()}
    problems, admitted = [], set()
    covered = {host: False, wildcard: False}
    for app in cf.apps():
        hosts = _app_hosts(app) & set(covered)
        if not hosts:
            continue
        if app.get("type") != "self_hosted":
            problems.append(f"Access app {app.get('name')!r} on {sorted(hosts)} is not self_hosted")
            continue
        for p in app.get("policies") or []:
            full = {**policies.get(p.get("id"), {}), **p}
            emails = _policy_emails(full)
            if emails is None:
                problems.append(f"policy {full.get('name')!r} on {APP_NAME} is not an email allow-list "
                                f"(decision {full.get('decision')!r})")
            else:
                admitted |= emails
        if app.get("policies"):
            for h in hosts:
                covered[h] = True
    problems += [f"no Access app protects {h}; run `signal-radar deploy --setup`" for h, ok in covered.items() if not ok]
    out = {"protected": not problems, "admitted": len(admitted), "problems": problems}
    if viewers is not None:
        out["in_sync_with_viewers_file"] = admitted == set(viewers)
    return out


def probe(url: str, client: httpx.Client | None = None) -> dict:
    """An anonymous request must be stopped by Access: a redirect to the login page, or 401/403."""
    client = client or httpx.Client(timeout=20)
    try:
        r = client.get(url, follow_redirects=False)
    except httpx.TransportError as e:
        # A new per-deployment hostname can fail TLS for some minutes while its certificate is issued. Nothing is
        # served, so it is not exposed; it is not verified either, so it is reported as unverified.
        return {"url": url, "status": None, "guarded": None, "error": type(e).__name__}
    loc = r.headers.get("location", "")
    guarded = (r.is_redirect and "cloudflareaccess.com" in loc) or r.status_code in (401, 403)
    return {"url": url, "status": r.status_code, "guarded": guarded}


# ------------------------------------------------------------------ deploy


def _write_headers(root: Path) -> None:
    (root / "_headers").write_text("/*\n  X-Robots-Tag: noindex, nofollow\n  Referrer-Policy: same-origin\n"
                                   "  X-Content-Type-Options: nosniff\n")


def wrangler_upload(cfg: PublishConfig, root: Path, account: str, token: str, message: str) -> str:
    """Direct upload with wrangler (Cloudflare's supported uploader). Returns the per-deployment URL."""
    c = cfg.cloudflare
    cmd = [*c.wrangler, "pages", "deploy", str(root), "--project-name", c.project,
           "--branch", c.production_branch, "--commit-dirty=true", "--commit-message", message]
    env = {**os.environ, "CLOUDFLARE_ACCOUNT_ID": account, "CLOUDFLARE_API_TOKEN": token,
           "WRANGLER_SEND_METRICS": "false"}
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900, cwd=PROJECT_ROOT)
    if r.returncode != 0:
        raise PublishError(f"wrangler failed ({r.returncode}): {(r.stderr or r.stdout)[-800:]}")
    m = re.search(r"https://[a-z0-9-]+\.[a-z0-9-]+\.pages\.dev", r.stdout + r.stderr)
    return m.group(0) if m else ""


def deploy(cfg: PublishConfig | None = None, dry_run: bool = False, build: bool = True, root: Path | None = None,
           narrate: bool = True, log=lambda m: None, cf: Cloudflare | None = None) -> dict:
    """Build the public edition, check it, and (unless dry_run) upload it behind Access and probe it."""
    cfg = cfg or load_publish()
    root = root or PUBLIC_DIR
    if build:
        from .site.build import build_site

        build_site(root, edition="public", narrate=narrate, log=log, exclude_sources=cfg.exclude_sources)
    _write_headers(root)
    bundle = check_bundle(root, cfg.exclude_sources)
    summary = plan(bundle, cfg)
    if bundle.problems:
        raise PublishError("not published, the bundle failed its checks:\n  " + "\n  ".join(bundle.problems))
    try:
        viewers = read_viewers(PROJECT_ROOT / cfg.cloudflare.viewers_file)
        summary["viewers"] = len(viewers)
    except PublishError as e:
        if not dry_run:
            raise
        summary["viewers"] = str(e)
    if dry_run:
        return {"dry_run": True, **summary}

    account, token = credentials(cfg)
    cf = cf or Cloudflare(account, token)
    project = cf.project(cfg.cloudflare.project)
    if project is None:
        raise PublishError(f"Pages project {cfg.cloudflare.project} does not exist: run `signal-radar deploy --setup`")
    access = verify_access(cf, project, viewers)
    if not access["protected"]:
        raise PublishError("not published, access control is missing:\n  " + "\n  ".join(access["problems"]))
    host, _ = hostnames(project)
    summary["url"] = f"https://{host}"
    log(f"uploading {bundle.files} files to {host}")
    deployment = wrangler_upload(cfg, root, account, token, f"Signal Radar data to {summary['data_to']}")
    # the page and the data behind it, on the production and the per-deployment hostname
    probes = [probe(f"{u}{path}") for u in (summary["url"], deployment) if u for path in ("/", "/data/snapshot.json")]
    exposed = [p["url"] for p in probes if p["guarded"] is False]
    result = {"dry_run": False, **summary, "deployment": deployment, "access": access, "probes": probes,
              "unverified": [p["url"] for p in probes if p["guarded"] is None]}
    if exposed:
        raise PublishError(f"PUBLISHED BUT NOT PROTECTED: {exposed} answered without a login. "
                           f"Check the Access app in the Zero Trust dashboard now. {json.dumps(probes)}")
    return result
