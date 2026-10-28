import json
import plistlib

import httpx
import pytest

from signal_radar import publish as pb
from signal_radar import schedule as sch
from signal_radar.items import Item, item_id
from signal_radar.signals import public_store
from signal_radar.site.build import build_site
from test_signals import store  # noqa: F401 - pytest fixture
from test_site import built  # noqa: F401 - pytest fixture

CFG = pb.PublishConfig.model_validate({
    "publisher": "Example B.V.", "notice": "© {year} {publisher} · Preview.",
    "cloudflare": {"project": "sr-test", "token_keyring_service": None}})


def public_db(corpus, tmp_path):
    return public_store(frozenset(CFG.exclude_sources), corpus=corpus, main=tmp_path / "signals.sqlite",
                        path=tmp_path / "public.sqlite")


@pytest.fixture
def public(built, tmp_path):  # noqa: F811
    corpus, _ = built
    db = public_db(corpus, tmp_path)
    out = tmp_path / "pub"
    build_site(out, edition="public", narrate=False, db=db)
    pb._write_headers(out)
    return out, db


def test_public_store_leaves_out_excluded_sources_entirely(built, tmp_path):  # noqa: F811
    """ADR-0015: AlphaSignal items, their mentions and their co-coverage of other items are gone, not hidden."""
    corpus, full = built
    tldr = next(m for m, _ in corpus.iter_meta() if m["source"] == "tldr_ai")
    corpus.write([Item(id=item_id(tldr["url"], "Same story"), source_id="alphasignal", date=tldr["date"],
                       url=tldr["url"], title="Same story", engagement={"likes": 9})])   # -> also_in on the TLDR item
    db = public_db(corpus, tmp_path)
    assert full.execute("SELECT COUNT(*) FROM items WHERE source = 'alphasignal'").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM items WHERE source = 'alphasignal' OR families LIKE '%alphasignal%'"
                      ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM mentions WHERE source = 'alphasignal'").fetchone()[0] == 0
    assert db.execute("SELECT sources_count FROM items WHERE id = ?", (tldr["id"],)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == full.execute(
        "SELECT COUNT(*) FROM items WHERE source <> 'alphasignal'").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0   # votes stay private


def test_bundle_naming_an_excluded_source_is_refused(built, tmp_path):  # noqa: F811
    _, full = built
    out = tmp_path / "leaky"
    build_site(out, edition="public", narrate=False, db=full)   # the full store: AlphaSignal counts included
    problems = " ".join(pb.check_bundle(out, CFG.exclude_sources).problems)
    assert "names excluded source" in problems and "snapshot.json" in problems


class FakeCloudflare:
    """In-memory Cloudflare API behind httpx.MockTransport: pages projects, IdPs, policies, apps."""

    def __init__(self, project=True):
        self.projects = {"sr-test": {"name": "sr-test", "subdomain": "sr-test-x1.pages.dev"}} if project else {}
        self.idps, self.policies, self.apps, self.calls = [], [], [], []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/accounts/acc", 1)[1]
        self.calls.append((req.method, path))
        body = json.loads(req.content) if req.content else None
        ok = lambda r: httpx.Response(200, json={"success": True, "errors": [], "result": r})  # noqa: E731
        if path.startswith("/pages/projects/"):
            p = self.projects.get(path.rsplit("/", 1)[1])
            return ok(p) if p else httpx.Response(404, json={"success": False, "errors": [{"code": 8000007}]})
        if path == "/pages/projects":
            self.projects[body["name"]] = {**body, "subdomain": f"{body['name']}.pages.dev"}
            return ok(self.projects[body["name"]])
        coll = {"/access/identity_providers": self.idps, "/access/policies": self.policies, "/access/apps": self.apps}
        base = next((k for k in coll if path.startswith(k)), None)
        items = coll[base]
        if req.method == "GET":
            return ok(items)
        if req.method == "POST":
            items.append({**body, "id": f"{base.rsplit('/', 1)[1]}-{len(items) + 1}"})
            return ok(items[-1])
        obj_id = path.rsplit("/", 1)[1]
        i = next(n for n, x in enumerate(items) if x["id"] == obj_id)
        items[i] = {**body, "id": obj_id}
        return ok(items[i])

    def client(self) -> pb.Cloudflare:
        return pb.Cloudflare("acc", "tok", httpx.Client(base_url=pb.API, transport=httpx.MockTransport(self.handler)))


def test_public_bundle_passes_and_carries_footer(public):
    out, _ = public
    b = pb.check_bundle(out, CFG.exclude_sources)
    assert b.problems == [] and b.by_kind["entity"] >= 1
    assert not (out / "static" / "console.js").exists()
    html = (out / "index.html").read_text()
    assert 'name="robots" content="noindex' in html
    summary = pb.plan(b, CFG)
    assert summary["files"] == b.files and summary["data_to"]


def test_analytics_feedback_and_privacy_only_in_public_and_only_when_set(built, tmp_path, monkeypatch):  # noqa: F811
    corpus, full = built
    db = public_db(corpus, tmp_path)
    monkeypatch.setattr(pb, "load_publish", lambda path=None: CFG)
    bare = tmp_path / "bare"
    build_site(bare, edition="public", narrate=False, db=db)
    method = (bare / "method.html").read_text()
    assert 'id="privacy"' in method and "reply to your invitation email" in method and "signed in for 7 days" in method
    assert "cloudflareinsights" not in (bare / "index.html").read_text() and "Send feedback" not in method

    cfg = CFG.model_copy(update={"web_analytics_token": "abc123", "feedback_url": "https://tally.so/r/x",
                                 "privacy_contact": "privacy@example.com"})
    monkeypatch.setattr(pb, "load_publish", lambda path=None: cfg)
    out = tmp_path / "full"
    build_site(out, edition="public", narrate=False, db=db)
    for page in ("index.html", "brief.html", "explore.html", "method.html"):
        html = (out / page).read_text()
        assert 'data-cf-beacon=\'{"token": "abc123"}\'' in html and 'href="https://tally.so/r/x"' in html
        assert 'href="method.html#privacy"' in html
    method = (out / "method.html").read_text()
    assert "Visit statistics" in method and "privacy@example.com" in method and "Feedback." in method
    assert pb.check_bundle(out, cfg.exclude_sources).problems == []

    pages = CFG.model_copy(update={"web_analytics_pages": True})
    monkeypatch.setattr(pb, "load_publish", lambda path=None: pages)
    build_site(out, edition="public", narrate=False, db=db)
    assert "Visit statistics" in (out / "method.html").read_text()        # Cloudflare injects the beacon itself
    assert "cloudflareinsights" not in (out / "index.html").read_text()    # so we must not add a second one

    mail = cfg.model_copy(update={"feedback_url": "mailto:a@example.com?subject=x"})
    monkeypatch.setattr(pb, "load_publish", lambda path=None: mail)
    build_site(out, edition="public", narrate=False, db=db)
    method = (out / "method.html").read_text()
    assert 'href="mailto:a@example.com?subject=x">Send feedback' in method and "goes by email" in method

    con = tmp_path / "con"
    build_site(con, edition="console", narrate=False, db=full)
    assert not any(x in (con / "method.html").read_text() for x in ("cloudflareinsights", 'id="privacy"', "tally.so"))


def test_console_build_and_strays_are_refused(built, tmp_path):  # noqa: F811
    _, db = built
    con = tmp_path / "con"
    build_site(con, edition="console", narrate=False, db=db)
    problems = " ".join(pb.check_bundle(con).problems)
    assert "console.js" in problems and "private fields" in problems
    pub = tmp_path / "pub"
    build_site(pub, edition="public", narrate=False, db=db)
    (pub / "signals.sqlite").write_text("x")
    (pub / "data" / "entity" / "..evil.json").write_text("{}")
    (pub / "data" / "extra.json").write_text(json.dumps({"items": [{"summary": "newsletter text"}]}))
    problems = pb.check_bundle(pub).problems
    assert "unexpected file: signals.sqlite" in problems
    assert "unexpected file: data/entity/..evil.json" in problems
    assert any("data/extra.json has private fields ['summary']" in p for p in problems)


def test_read_viewers(tmp_path):
    f = tmp_path / "viewers.txt"
    f.write_text("# friends\nA@Example.com\n\na@example.com  # duplicate\nb@example.org\n")
    assert pb.read_viewers(f) == ["a@example.com", "b@example.org"]
    f.write_text("not-an-email\n")
    with pytest.raises(pb.PublishError, match="line 1"):
        pb.read_viewers(f)
    with pytest.raises(pb.PublishError, match="no viewer list"):
        pb.read_viewers(tmp_path / "missing.txt")


def test_setup_is_idempotent_and_protects_both_hostnames():
    fake = FakeCloudflare(project=False)
    cf = fake.client()
    res = pb.setup(CFG, cf, ["a@example.com"])
    assert res["url"] == "https://sr-test.pages.dev" and len(fake.apps) == 1
    app = fake.apps[0]
    assert {d["uri"] for d in app["destinations"]} == {"sr-test.pages.dev", "*.sr-test.pages.dev"}
    assert app["allowed_idps"] == [fake.idps[0]["id"]] and fake.idps[0]["type"] == "onetimepin"
    pb.setup(CFG, cf, ["a@example.com", "b@example.com"])  # second run updates, never duplicates
    assert len(fake.apps) == len(fake.policies) == len(fake.idps) == 1
    assert len(fake.policies[0]["include"]) == 2
    check = pb.verify_access(cf, fake.projects["sr-test"], ["a@example.com", "b@example.com"])
    assert check == {"protected": True, "admitted": 2, "problems": [], "in_sync_with_viewers_file": True}


def test_verify_access_catches_gaps():
    fake = FakeCloudflare()
    cf = fake.client()
    project = fake.projects["sr-test"]
    assert not pb.verify_access(cf, project)["protected"]  # nothing configured
    pb.setup(CFG, cf, ["a@example.com"])
    fake.apps[0]["destinations"] = [{"type": "public", "uri": "sr-test-x1.pages.dev"}]
    assert any("*.sr-test-x1.pages.dev" in p for p in pb.verify_access(cf, project)["problems"])
    pb.setup(CFG, cf, ["a@example.com"])
    fake.policies[0]["include"].append({"everyone": {}})
    assert "not an email allow-list" in " ".join(pb.verify_access(cf, project)["problems"])


def test_probe():
    login = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://team.cloudflareaccess.com/x"}))
    open_ = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    assert pb.probe("https://a.pages.dev", httpx.Client(transport=login))["guarded"]
    assert pb.probe("https://a.pages.dev", httpx.Client(transport=open_))["guarded"] is False

    def tls_fails(r):
        raise httpx.ConnectError("handshake failure")
    unreachable = pb.probe("https://abc.a.pages.dev", httpx.Client(transport=httpx.MockTransport(tls_fails)))
    assert unreachable["guarded"] is None and unreachable["status"] is None  # unverified, not exposed


def test_deploy_refuses_without_access_and_fails_loudly_when_exposed(public, tmp_path, monkeypatch):
    out, _ = public
    viewers = tmp_path / "config" / "viewers.txt"
    viewers.parent.mkdir()
    viewers.write_text("a@example.com\n")
    monkeypatch.setattr(pb, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pb, "credentials", lambda cfg: ("acc", "tok"))
    uploads = []
    monkeypatch.setattr(pb, "wrangler_upload", lambda *a: uploads.append(a) or "https://abc.sr-test-x1.pages.dev")

    dry = pb.deploy(CFG, dry_run=True, build=False, root=out)
    assert dry["dry_run"] and dry["viewers"] == 1 and uploads == []

    fake = FakeCloudflare()
    with pytest.raises(pb.PublishError, match="access control is missing"):
        pb.deploy(CFG, build=False, root=out, cf=fake.client())
    assert uploads == []  # nothing left the machine

    pb.setup(CFG, fake.client(), ["a@example.com"])
    monkeypatch.setattr(pb, "probe", lambda u: {"url": u, "status": 302, "guarded": True})
    res = pb.deploy(CFG, build=False, root=out, cf=fake.client())
    assert res["url"] == "https://sr-test-x1.pages.dev" and len(uploads) == 1 and all(p["guarded"] for p in res["probes"])
    assert len(res["probes"]) == 4 and res["unverified"] == []  # page + data, production + deployment

    monkeypatch.setattr(pb, "probe", lambda u: {"url": u, "status": None, "guarded": None, "error": "ConnectError"})
    assert len(pb.deploy(CFG, build=False, root=out, cf=fake.client())["unverified"]) == 4  # reported, not raised

    monkeypatch.setattr(pb, "probe", lambda u: {"url": u, "status": 200, "guarded": False})
    with pytest.raises(pb.PublishError, match="NOT PROTECTED"):
        pb.deploy(CFG, build=False, root=out, cf=fake.client())


def test_footer_from_config():
    assert CFG.footer(2026) == "© 2026 Example B.V. · Preview."
    assert CFG.daily is False  # publishing from `daily` is opt-in unless publish.yaml turns it on
    assert pb.load_publish().cloudflare.project  # the real config parses


def test_launchd_plist_runs_daily_from_project_root():
    spec = sch.plist("daily", exe="/venv/bin/signal-radar")
    assert spec["ProgramArguments"] == ["/venv/bin/signal-radar", "daily", "--catch-up"]
    assert spec["StartCalendarInterval"] == {"Hour": 7, "Minute": 30} and spec["RunAtLoad"] is True
    assert spec["WorkingDirectory"] == str(sch.PROJECT_ROOT) and spec["StandardOutPath"].endswith("daily.log")
    assert "/usr/bin" in spec["EnvironmentVariables"]["PATH"]
    plistlib.dumps(spec)  # serialisable as a launchd plist


def test_catch_up_skips_only_after_a_recent_success(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from signal_radar import cli

    monkeypatch.setattr(sch, "DATA_DIR", tmp_path)
    assert sch.hours_since_success("daily") is None            # never ran: catch up
    sch.mark_success("daily")
    assert sch.hours_since_success("daily") < 0.1
    args = cli.build_parser().parse_args(["daily", "--catch-up"])
    assert args.func(args) == 0                                  # skipped, no ingest attempted
    sch._stamp("daily").write_text((datetime.now(UTC) - timedelta(hours=30)).isoformat())
    assert sch.hours_since_success("daily") > sch.CATCH_UP_HOURS  # stale: the login run would do the work
