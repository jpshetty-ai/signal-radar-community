"""Static site builder (ADR-0011). One snapshot of the signal store -> HTML + JSON in an output folder.

Editions:
- console: the private edition. Includes newsletter summaries, the Ask drawer and feedback buttons, which work
  when the folder is served by `signal-radar web` (they call its /api routes).
- public: read-only. No newsletter summaries (copyright: only titles linking to the original article, plus our
  own data), no Ask, no feedback. Deployable to any static host.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

from ..config import DATA_DIR
from ..extract import Taxonomy, connect
from .data import build_snapshot, dump, entity_detail, entity_index

HERE = Path(__file__).parent
SITE_DIR = DATA_DIR / "site"
EDITIONS = ("console", "public")


def _brief_for(edition: str) -> dict | None:
    from ..brief import latest_brief

    brief = latest_brief(edition)  # the public brief is computed from the public store (ADR-0015)
    if brief and edition == "public":  # our own prose stays; newsletter summary text does not, wherever it sits
        brief = _drop_key(brief, "summary")  # (signal evidence, lens links, lens items: any field added later too)
    if brief:
        brief["by_sid"] = {s["sid"]: s for s in brief["signals"]}
    return brief


def _drop_key(obj, key: str):
    if isinstance(obj, dict):
        return {k: _drop_key(v, key) for k, v in obj.items() if k != key}
    if isinstance(obj, list):
        return [_drop_key(v, key) for v in obj]
    return obj


def _strip_for_public(snap: dict) -> dict:
    for t in snap["themes"]:
        for i in t["items"]:
            i.pop("summary", None)
    for r in snap["reading"]:
        r.pop("summary", None)
        r.pop("vote", None)
    return snap


PAGES = (("today", "index.html"), ("brief", "brief.html"), ("explore", "explore.html"), ("method", "method.html"))


def public_exclusions() -> list[str]:
    """Source ids left out of the public edition (config/publish.yaml `exclude_sources`, ADR-0015)."""
    from ..publish import DEFAULT_EXCLUDED, load_publish

    try:
        return load_publish().exclude_sources
    except FileNotFoundError:
        return DEFAULT_EXCLUDED


def public_db(exclude_sources: list[str] | None = None):
    from ..signals import public_store

    return public_store(frozenset(public_exclusions() if exclude_sources is None else exclude_sources))


def public_settings() -> dict:
    """Footer, analytics, feedback and privacy settings for the public edition, from config/publish.yaml."""
    try:
        from ..publish import load_publish

        cfg = load_publish()
    except FileNotFoundError:
        return {}
    return {"footer": cfg.footer(), "publisher": cfg.publisher, "analytics_token": cfg.web_analytics_token,
            "analytics": bool(cfg.web_analytics_token or cfg.web_analytics_pages),
            "feedback_url": cfg.feedback_url, "privacy_contact": cfg.privacy_contact,
            "session_hours": int(cfg.cloudflare.session_duration.rstrip("h") or 0)}


def public_footer() -> str | None:
    return public_settings().get("footer")


def _env(single: bool) -> Environment:
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    files = {"today": "index.html", "brief": "brief.html", "explore": "explore.html", "method": "method.html"}

    def href(page: str, frag: str | None = None) -> str:
        if single:  # one document, views switched by the hash
            return f"#{page}:{frag}" if frag else f"#{page}"
        return f"{files[page]}#{frag}" if frag else files[page]

    env.globals["href"] = href
    return env


def build_single_file(out_file: Path | None = None, db=None, narrate: bool = True, complete=None,
                      note: str | None = None, exclude_sources: list[str] | None = None) -> dict:
    """The public edition as one self-contained HTML file: CSS, scripts and all data inline.

    For sharing with a few people: open it in any browser, no server. Explore works because the entity
    neighbourhoods are embedded instead of fetched (browsers block fetch() on files). Fonts still load from
    Google Fonts when online and fall back to system fonts offline. Built from the public store, so excluded
    sources are absent (ADR-0015).
    """
    db = db if db is not None else public_db(exclude_sources)
    note = note or public_footer() or "Confidential snapshot for named recipients. Do not forward."
    snap = _strip_for_public(build_snapshot(db, Taxonomy.load(), narrate=narrate, complete=complete))
    snap["brief"] = _brief_for("public")
    index = entity_index(db)
    data = {"entities": index, "entity": {e["slug"]: entity_detail(db, e["name"]) for e in index}}
    env = _env(single=True)
    mains = {}
    for page, template in PAGES:
        html = env.get_template(template).render(**snap, page=page, edition="public")
        mains[page] = html.split('<main class="wrap">', 1)[1].rsplit("</main>", 1)[0]
        if page == "today":
            header = html[html.index('<header class="bar">'):html.index("</header>") + len("</header>")]
    css = (HERE / "static" / "radar.css").read_text()
    js = (HERE / "static" / "explore.js").read_text()
    payload = dump(data).replace("</", "<\\/")
    built = snap["meta"]["built_at"][:10]
    views = "\n".join(
        f'<main class="wrap view" id="view-{p}"{"" if p == "today" else " hidden"}>{mains[p]}</main>' for p, _ in PAGES)
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Radar · {snap["meta"]["as_of"]}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600&family=Martian+Mono:wdth,wght@75..112.5,400..600&display=swap">
<meta name="robots" content="noindex, nofollow">
<style>{css}</style>
</head>
<body data-edition="public">
{header}
{views}
<footer class="wrap share-note">{escape(note)} Built {built} from data to {snap["meta"]["as_of"]}.</footer>
<script id="sr-data" type="application/json">{payload}</script>
<script>window.SR_DATA = JSON.parse(document.getElementById("sr-data").textContent);</script>
<script>
(() => {{
  const views = ["today", "brief", "explore", "method"];
  let last = null;
  function route() {{
    const h = location.hash.slice(1);
    const v = h.startsWith("explore") ? "explore" : (["method", "brief"].includes(h) ? h : "today");
    views.forEach(x => {{ document.getElementById("view-" + x).hidden = x !== v; }});
    document.querySelectorAll("nav a[data-view]").forEach(a => {{
      if (a.dataset.view === v) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }});
    if (v !== last) window.scrollTo(0, 0);
    last = v;
  }}
  window.addEventListener("hashchange", route);
  route();
}})();
</script>
<script>{js}</script>
</body>
</html>
"""
    out_file = out_file or DATA_DIR / "share" / f"signal-radar-{snap['meta']['as_of']}.html"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(doc)
    return {"out": str(out_file), "bytes": out_file.stat().st_size, "entities": len(index),
            "themes": [t["title"] for t in snap["themes"]]}


def build_site(out: Path | None = None, edition: str = "console", narrate: bool = True, db=None, complete=None,
               log=lambda m: None, exclude_sources: list[str] | None = None) -> dict:
    if edition not in EDITIONS:
        raise ValueError(f"edition must be one of {EDITIONS}")
    out = out or (SITE_DIR if edition == "console" else DATA_DIR / "site_public")
    if db is None:  # the public edition never sees excluded sources, not even in counts (ADR-0015)
        db = public_db(exclude_sources) if edition == "public" else connect()
    tax = Taxonomy.load()
    snap = build_snapshot(db, tax, narrate=narrate, complete=complete)
    if edition == "public":
        snap = _strip_for_public(snap)
    snap["brief"] = _brief_for(edition)

    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "data" / "entity").mkdir(parents=True)
    # the public edition has no Ask or feedback, so it does not ship their script either
    shutil.copytree(HERE / "static", tmp / "static",
                    ignore=shutil.ignore_patterns("console.js") if edition == "public" else None)

    index = entity_index(db)
    (tmp / "data" / "entities.json").write_text(dump(index))
    for e in index:
        (tmp / "data" / "entity" / f"{e['slug']}.json").write_text(dump(entity_detail(db, e["name"])))
    (tmp / "data" / "snapshot.json").write_text(dump(snap))
    log(f"{len(index)} entity pages")

    env = _env(single=False)
    site = public_settings() if edition == "public" else {}
    for page, template in PAGES:
        html = env.get_template(template).render(**snap, page=page, edition=edition, footer=site.get("footer"), site=site)
        (tmp / template).write_text(html)

    shutil.rmtree(out, ignore_errors=True)  # swap in the finished build so a server never serves half a site
    tmp.rename(out)
    return {"out": str(out), "edition": edition, "themes": [t["title"] for t in snap["themes"]],
            "reading": len(snap["reading"]), "entities": len(index)}
