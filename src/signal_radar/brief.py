"""Weekly signal brief (CLAUDE.md "Analysis jobs"): computed signals, model-written prose, evidence everywhere.

Rule: never invent signals. Every section is computed from the signal store first; one model call (role
`writing`) then writes only the prose: a headline, a "so what for me" line per signal, learning suggestions and
opportunity ideas, each restricted to the evidence it is given. Prose that cites an unknown signal or item is
dropped. Thin evidence is labelled, not hidden.

Outputs: briefs/<ISO week>.md (Git-tracked, exportable) and data/briefs/<ISO week>.json (for the console).
The public edition gets its own brief, data/briefs/<ISO week>.public.json, computed from the public store and
written by its own prose call: prose written over excluded sources must not reach the public (ADR-0015).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import signals as sg
from .config import DATA_DIR, PROJECT_ROOT
from .extract import Taxonomy

BRIEFS_DIR = PROJECT_ROOT / "briefs"
BRIEF_DATA_DIR = DATA_DIR / "briefs"
NEW_TYPES = ("Company", "Product", "Technology", "Regulation", "Concept")
LEARN_TYPES = ("Technology", "Concept", "Product")

PROSE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "signals": {"type": "array", "items": {"type": "object", "properties": {
            "sid": {"type": "string"}, "so_what": {"type": "string"}},
            "required": ["sid", "so_what"], "additionalProperties": False}},
        "learning": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
            "sid": {"type": "string"}, "why": {"type": "string"}, "project": {"type": "string"}},
            "required": ["sid", "why", "project"], "additionalProperties": False}},
        "opportunities": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
            "idea": {"type": "string"}, "target_client": {"type": "string"}, "problem": {"type": "string"},
            "why_now": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["idea", "target_client", "problem", "why_now", "evidence_ids"], "additionalProperties": False}},
    },
    "required": ["headline", "signals", "learning", "opportunities"],
    "additionalProperties": False,
}


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def weekly_due(today: date | None = None, folder: Path | None = None) -> date | None:
    """The Monday that ends this ISO week's brief window, or None when briefs/<this week>.md already exists.

    `daily` calls this so the first successful run of each week writes the brief; a Mac that was off on
    Monday catches up on the next run, and the window stays Tuesday to Monday whatever day that is.
    """
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    return None if ((folder or BRIEFS_DIR) / f"{iso_week(monday)}.md").exists() else monday


def _items(db: sqlite3.Connection, ids: list[str], limit: int = 4) -> list[dict]:
    if not ids:
        return []
    rows = db.execute(f"""SELECT id, date, source, title, url, relevance, summary FROM items WHERE id IN ({','.join('?' * len(ids))})
        ORDER BY COALESCE(relevance, 0) DESC, sources_count DESC, date DESC LIMIT ?""", [*ids, limit]).fetchall()
    return [{"id": r[0], "date": r[1], "source": r[2], "title": r[3], "url": r[4], "relevance": r[5],
             "summary": (r[6] or "")[:280]} for r in rows]


def _entity_items(db: sqlite3.Connection, name: str, start: str, end: str, limit: int = 4) -> list[dict]:
    ids = [r[0] for r in db.execute("""SELECT m.item_id FROM mentions m JOIN entities e ON e.id = m.entity_id
        WHERE e.name = ? AND m.date BETWEEN ? AND ?""", (name, start, end))]
    return _items(db, ids, limit)


def collect(db: sqlite3.Connection, end: date | None = None, days: int = 7) -> dict:
    """All computed sections of the brief. No model involved."""
    from .site.data import watch_count

    end = end or sg.as_of_default(db)
    start = end - timedelta(days=days - 1)
    span = (start.isoformat(), end.isoformat())
    signals, n = [], 0

    def add(kind: str, **kw) -> dict:
        nonlocal n
        n += 1
        s = {"sid": f"S{n}", "kind": kind, **kw}
        signals.append(s)
        return s

    for m in sg.top_movers(db, days, limit=12, min_current=3, min_relevance=1, as_of=end):
        if len(m.entity) < 3:
            continue  # "US", "EU": places and abbreviations, not signals
        add("rising" if m.confidence != "low" else "watching", name=m.entity, type=m.type, current=m.current,
            baseline=m.baseline, confidence=m.confidence, families=m.families, new=m.new,
            evidence=_entity_items(db, m.entity, *span))
    rising_names = {s["name"] for s in signals}
    for name, etype, cur, fams in db.execute(f"""SELECT e.name, e.type, COUNT(*), COUNT(DISTINCT m.family) FROM mentions m
            JOIN entities e ON e.id = m.entity_id JOIN items i ON i.id = m.item_id
            WHERE e.first_seen BETWEEN ? AND ? AND m.date BETWEEN ? AND ? AND COALESCE(i.relevance, 0) >= 1
            AND e.type IN ({','.join('?' * len(NEW_TYPES))})
            GROUP BY e.id HAVING COUNT(*) >= 2 ORDER BY COUNT(*) DESC, e.name LIMIT 20""", (*span, *span, *NEW_TYPES)):
        if name in rising_names or not (name[0].isupper() or name[0].isdigit()) or len(name) < 3:
            continue  # lower-case names are generic nouns the extraction let through
        if sum(s["kind"] == "new" for s in signals) >= 6:
            break
        add("new", name=name, type=etype, current=cur, families=fams, confidence="low" if fams < 2 else "medium",
            evidence=_entity_items(db, name, *span))
    for m in sg.top_movers(db, days, limit=5, direction="down", as_of=end):
        add("fading", name=m.entity, type=m.type, current=m.current, baseline=m.baseline, confidence=m.confidence,
            families=m.families, evidence=m.evidence[:2])
    graph = _graph_signals(db, end, days, add)
    if graph["clusters"] is None:  # Neo4j unreachable: pairs from the signal store, as before ADR-0018
        for c in sg.cooccurrence_growth(db, days, periods=4, limit=8, min_current=3, as_of=end):
            a, b = c["pair"]
            ids = [r[0] for r in db.execute("""SELECT a.item_id FROM mentions a JOIN mentions b ON a.item_id = b.item_id
                JOIN entities ea ON ea.id = a.entity_id JOIN entities eb ON eb.id = b.entity_id
                WHERE ea.name = ? AND eb.name = ? AND a.date BETWEEN ? AND ?""", (a, b, *span))]
            add("cluster", name=f"{a} + {b}", pair=[a, b], current=c["current"], baseline=c["baseline"],
                confidence="medium" if c["current"] >= 4 else "low", evidence=_items(db, ids, 3))
            if sum(s["kind"] == "cluster" for s in signals) >= 4:
                break
    _attach_lens(db, signals, graph, end)

    lens_items = _items(db, [r[0] for r in db.execute("""SELECT id FROM items WHERE date BETWEEN ? AND ?
        AND (category IN ('regulation_governance', 'financial_services') OR COALESCE(relevance, 0) >= 2)""", span)], 8)
    reading = sg.reading_list(db, days=days, limit=6, as_of=end)
    tax = Taxonomy.load()
    texts_week = [f"{t} {s or ''}" for t, s in db.execute("SELECT title, summary FROM items WHERE date BETWEEN ? AND ?", span)]
    coverage = {
        "items": len(texts_week),
        "by_source": dict(db.execute("SELECT source, COUNT(*) FROM items WHERE date BETWEEN ? AND ? GROUP BY source ORDER BY 2 DESC",
                                     span).fetchall()),
        "watchlist": [{"term": t, "week": watch_count(t, texts_week)} for t in tax.watchlist],
    }
    learning_ids = [s["sid"] for s in signals if s["kind"] in ("rising", "watching", "new") and s.get("type") in LEARN_TYPES]
    if graph["close"]:
        graph["close"]()
    return {"week": iso_week(end), "start": span[0], "end": span[1], "days": days, "signals": signals,
            "graph": graph["status"], "lens_targets": graph["targets"], "lens_items": lens_items, "reading": reading, "coverage": coverage, "learning_candidates": learning_ids}


def _graph_signals(db: sqlite3.Connection, end: date, days: int, add: Callable) -> dict:
    """Story clusters from Neo4j (ADR-0018), added as "cluster" signals. Status says why when unavailable."""
    from .graph_signals import cluster_name, neo4j_reader, present_targets, story_clusters

    out = {"status": "neo4j", "clusters": None, "run": None, "close": None, "targets": None}
    try:
        out["run"], out["close"] = neo4j_reader()
        out["clusters"] = story_clusters(db, out["run"], end, days)
        present, missing = present_targets(out["run"], Taxonomy.load().lens_targets)
        out["targets"] = {"present": present, "missing": missing}
    except Exception as e:  # noqa: BLE001 - the brief still runs; clusters fall back to pairs, no lens links
        out["status"] = f"unavailable: {type(e).__name__}: {e}"[:200]
        out["clusters"] = None
        return out
    for c in out["clusters"]:
        add("cluster", name=cluster_name(c["entities"]), entities=c["entities"], links=c["links"],
            current=c["current"], families=c["families"], new=c["new"], confidence=c["confidence"],
            evidence=_items(db, c["doc_ids"], 3))
    return out


def _attach_lens(db: sqlite3.Connection, signals: list[dict], graph: dict, end: date) -> None:
    """Each signal's lens link: articles that mention it together with a lens target. None = not checked."""
    if graph["run"] is None or graph["status"] != "neo4j":
        for s in signals:
            s["lens"] = None
        return
    from .graph_signals import lens_links

    names = sorted({n for s in signals for n in (s.get("entities") or s.get("pair") or [s["name"]])})
    links = lens_links(db, graph["run"], names, Taxonomy.load().lens_targets, end)
    for s in signals:
        merged: dict[str, dict] = {}
        for n in s.get("entities") or s.get("pair") or [s["name"]]:
            for link in links.get(n, []):
                m = merged.setdefault(link["target"], {"target": link["target"], "self": False, "doc_ids": set()})
                m["self"] = m["self"] or bool(link.get("self"))
                m["doc_ids"].update(link["doc_ids"])
        ranked = sorted(merged.values(), key=lambda m: (not m["self"], -len(m["doc_ids"]), m["target"]))[:3]
        s["lens"] = [{"target": m["target"], "self": m["self"], "articles": len(m["doc_ids"]),
                      "evidence": _items(db, sorted(m["doc_ids"]), 2)} for m in ranked]


# ------------------------------------------------------------------------------------------------- prose


def _lens_line(s: dict) -> str:
    if s.get("lens") is None:
        return "not checked (graph unavailable): treat as none"
    if not s["lens"]:
        return "none"
    parts = []
    for link in s["lens"]:
        if link["self"]:
            parts.append(f"is itself {link['target']}")
        else:
            ids = ", ".join(f"[{e['id']}]" for e in link["evidence"])
            parts.append(f"{link['target']} in {link['articles']} article(s) {ids}")
    return "; ".join(parts)


# A "so what" that ties a signal to the reader's market although the graph found no link is an invented claim.
LENS_CLAIM = re.compile(r"\b(financ\w*|bank\w*|insur\w*|fintech\w*|payments?|complian\w*|regulat\w*|supervis\w*|"
                        r"EU AI Act|DORA|GDPR|MiCA|Basel)\b", re.I)


def prose_prompt(data: dict, lens: str) -> tuple[str, str]:
    from .site.prose import STYLE

    lines = []
    for s in data["signals"]:
        counts = f"{s['current']} mentions" + (f" vs {s['baseline']:g}/week before" if "baseline" in s else "")
        kind = "story" if s.get("entities") else s.get("type", "pair")
        lines.append(f"{s['sid']} [{s['kind']}] {s['name']} ({kind}): {counts}, confidence {s['confidence']}")
        for e in s["evidence"][:2]:  # compact: the prompt must fit a local model's 8k context with room to answer
            lines.append(f"    [{e['id']}] {e['title']}: {(e.get('summary') or '')[:150]}")
        lines.append(f"    lens link: {_lens_line(s)}")
    lens_lines = [f"[{e['id']}] {e['title']}: {(e.get('summary') or '')[:150]}" for e in data["lens_items"][:6]]
    system = ("You write a weekly market-intelligence brief for one reader. Use only the signals and items below; "
              f"do not add outside facts. Counts are small: do not call anything a trend unless the numbers say so. {STYLE}")
    prompt = f"""Reader: {lens}

Signals this week ({data['start']} to {data['end']}):
{chr(10).join(lines)}

Items rated relevant to the reader this week:
{chr(10).join(lens_lines) or "none"}

Learning candidates (signal ids): {", ".join(data['learning_candidates']) or "none"}

Return:
- headline: 3 or 4 sentences on what mattered this week for this reader, plainly. Mention when evidence is thin.
- signals: for each signal id above, one sentence "so what". Connect a signal to the reader's market (financial
  services, insurance, payments, risk, compliance, regulation) ONLY if its lens link names an article that does;
  build on that article. If the lens link is "none" or "not checked", do not mention the reader's market or those
  sectors at all, not even to say it does not apply: say what the signal means in general. Never invent a link.
- learning: up to 2, only from the learning candidates: sid, why it is worth learning now, and one concrete
  learning project of a few days.
- opportunities: up to 2 product, service or consulting ideas, each with target client, problem, why now and
  evidence_ids (item ids from above that support it). Skip this if the evidence does not support any."""
    return system, prompt


def write_prose(data: dict, lens: str, complete: Callable | None = None) -> dict:
    from .llm import LLMError
    from .site.prose import _clean, complete_with_fallback

    if complete is None:
        from .llm import LLMRouter

        complete = LLMRouter.from_config(job="brief").complete
    system, prompt = prose_prompt(data, lens)
    try:
        res, role = complete_with_fallback(complete, ("writing", "extraction"), prompt, system, PROSE_SCHEMA)
    except LLMError as e:
        return {"headline": "", "so_what": {}, "learning": [], "opportunities": [], "model": None, "error": str(e)[:200]}
    d = res.data or {}
    sids = {s["sid"] for s in data["signals"]}
    unlinked = {s["sid"] for s in data["signals"] if not s.get("lens")}
    dropped = {x["sid"]: x["so_what"] for x in d.get("signals", [])
               if x.get("sid") in unlinked and LENS_CLAIM.search(x.get("so_what") or "")}
    known_items = {e["id"] for s in data["signals"] for e in s["evidence"]} | {e["id"] for e in data["lens_items"]}
    return {
        "headline": _clean(d.get("headline", "")),
        "so_what": {x["sid"]: _clean(x["so_what"]) for x in d.get("signals", [])
                    if x.get("sid") in sids and x.get("so_what") and x["sid"] not in dropped},
        "dropped_lens_claims": dropped,
        "learning": [{"sid": x["sid"], "why": _clean(x["why"]), "project": _clean(x["project"])}
                     for x in d.get("learning", []) if x.get("sid") in set(data["learning_candidates"])][:2],
        "opportunities": [{**{k: _clean(x[k]) for k in ("idea", "target_client", "problem", "why_now")},
                           "evidence_ids": [i for i in x.get("evidence_ids", []) if i in known_items]}
                          for x in d.get("opportunities", []) if any(i in known_items for i in x.get("evidence_ids", []))][:2],
        "model": f"{res.provider}/{res.model}", "role": role,
    }


# ------------------------------------------------------------------------------------------------ render

_SRC = {"tldr_ai": "TLDR AI", "tldr_tech": "TLDR Tech", "tldr_founders": "TLDR Founders", "alphasignal": "AlphaSignal", "bayarealetters": "Bay Area Letters",
        "sifted": "Sifted", "techeu": "Tech.eu", "aiact_newsletter": "EU AI Act Newsletter", "ec_digital": "EC Digital Strategy"}


def _ev(items: list[dict], n: int = 3) -> str:
    return "; ".join(f"[{e['title']}]({e['url']}) ({_SRC.get(e['source'], e['source'])}, {e['date']})" for e in items[:n])


def _conf(s: dict) -> str:
    dots = {"high": "●●●", "medium": "●●○", "low": "●○○"}[s["confidence"]]
    fam = s.get("families")
    return f"{dots} {s['confidence']}" + (f", {fam} source famil{'y' if fam == 1 else 'ies'}" if fam else "")


CLUSTER_NOTE = ("Entities mentioned together for the first time, or far more often than in the four weeks before, "
                "grouped into stories by shared articles.")
PAIR_NOTE = "Pairs mentioned together more often than in the four weeks before (graph unavailable this week)."


def _lens_md(links: list[dict]) -> str:
    return "; ".join(f"is itself {x['target']}" if x["self"] else
                     f"{x['target']} ({x['articles']} article{'s' if x['articles'] != 1 else ''}: {_ev(x['evidence'], 1)})"
                     for x in links)


def render_markdown(b: dict) -> str:
    p = b.get("prose") or {}
    so = p.get("so_what", {})
    by_kind = {k: [s for s in b["signals"] if s["kind"] == k] for k in ("rising", "watching", "new", "fading", "cluster")}
    end = date.fromisoformat(b["end"])
    lines = [f"# Signal brief {b['week']}", "",
             f"Week to {end:%-d %B %Y} ({b['start']} to {b['end']}): {b['coverage']['items']} items from "
             f"{len(b['coverage']['by_source'])} sources. Signals are computed from the corpus; the prose is written by "
             f"{p.get('model') or 'no model (prose skipped)'} from the listed evidence only.", ""]
    if p.get("headline"):
        lines += ["## The week", "", p["headline"], ""]

    def block(title: str, items: list[dict], note: str = "") -> None:
        if not items:
            return
        lines.extend([f"## {title}", ""] + ([note, ""] if note else []))
        for s in items:
            counts = f"{s['current']} mentions" + (f" vs {s['baseline']:g} per week before" if "baseline" in s else "")
            new = " (new)" if s.get("new") else ""
            kind = "story" if s.get("entities") else s.get("type", "pair")
            lines.append(f"- **{s['name']}**{new}, {kind}: {counts}. Confidence {_conf(s)}.")
            if so.get(s["sid"]):
                lines.append(f"  So what: {so[s['sid']]}")
            if s.get("lens"):
                lines.append(f"  Lens link: {_lens_md(s['lens'])}")
            if s["evidence"]:
                lines.append(f"  Evidence: {_ev(s['evidence'])}")
        lines.append("")

    block("Rising", by_kind["rising"])
    block("Watching", by_kind["watching"], "Rising, but on three or so mentions: a hint, not a finding.")
    block("New this week", by_kind["new"])
    block("Fading", by_kind["fading"])
    block("Clusters forming", by_kind["cluster"], CLUSTER_NOTE if b.get("graph") == "neo4j" else PAIR_NOTE)
    if b["lens_items"]:
        lines += ["## Through your lens", ""] + [f"- {_ev([e], 1)}" for e in b["lens_items"]] + [""]
    sig = {s["sid"]: s for s in b["signals"]}
    if p.get("learning"):
        lines += ["## Learning radar", "",
                  "Built from rising technologies and concepts (skill extraction is still too thin to use).", ""]
        for x in p["learning"]:
            lines.append(f"- **{sig[x['sid']]['name']}**: {x['why']}  \n  Project: {x['project']}")
        lines.append("")
    if p.get("opportunities"):
        lines += ["## Opportunity ideas", ""]
        items = {e["id"]: e for s in b["signals"] for e in s["evidence"]} | {e["id"]: e for e in b["lens_items"]}
        for x in p["opportunities"]:
            lines += [f"- **{x['idea']}**", f"  Client: {x['target_client']}. Problem: {x['problem']}",
                      f"  Why now: {x['why_now']}", f"  Evidence: {_ev([items[i] for i in x['evidence_ids'] if i in items])}"]
        lines.append("")
    cov = b["coverage"]
    zero = [w["term"] for w in cov["watchlist"] if w["week"] == 0]
    lines += ["## Coverage", "",
              "Items this week by source: " + ", ".join(f"{_SRC.get(k, k)} {v}" for k, v in cov["by_source"].items()) + ".",
              f"Watchlist terms with no mention this week: {', '.join(zero) or 'none'}. A zero means the sources do not "
              "cover it, not that nothing happened.", ""]
    return "\n".join(lines)


def build_brief(db: sqlite3.Connection | None = None, end: date | None = None, days: int = 7, prose: bool = True,
                complete: Callable | None = None, out_md: Path | None = None, edition: str = "console") -> dict:
    from .extract import connect

    if db is None:
        if edition == "public":
            from .site.build import public_db

            db = public_db()
        else:
            db = connect()
    data = collect(db, end, days)
    data["prose"] = write_prose(data, Taxonomy.load().lens, complete) if prose else {}
    data["generated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    if edition == "console":  # the Git-tracked Markdown is the private record; the public brief is data only
        md = render_markdown(data)
        md = re.sub(r"[—–]", ",", md)  # house style holds even if a source title carries a dash
        out_md = out_md or BRIEFS_DIR / f"{data['week']}.md"
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md)
    BRIEF_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (BRIEF_DATA_DIR / _brief_file(data["week"], edition)).write_text(
        json.dumps(data, ensure_ascii=False, indent=1, default=str))
    return {"week": data["week"], "edition": edition, "markdown": str(out_md) if edition == "console" else None,
            "signals": len(data["signals"]), "model": (data["prose"] or {}).get("model")}


def _brief_file(week: str, edition: str) -> str:
    return f"{week}.json" if edition == "console" else f"{week}.{edition}.json"


def latest_brief(edition: str = "console", folder: Path | None = None) -> dict | None:
    files = sorted(p for p in (folder or BRIEF_DATA_DIR).glob("*.json") if p.name == _brief_file(p.name.split(".")[0], edition))
    return json.loads(files[-1].read_text()) if files else None
