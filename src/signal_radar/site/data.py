"""Site data: everything the pages show, computed once per build from the signal store.

Static-first (ADR-0011): the builder writes this as JSON next to the HTML. The private console renders the
same data and adds live parts (feedback, Ask) on top. A public edition renders it without newsletter summaries.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta

from .. import signals as sg
from ..extract import Taxonomy
from .prose import narrate_theme
from .themes import detect_themes

WEEKS = 12


def entity_slug(key: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", key).strip("-") or "e"


def weekly_series(db: sqlite3.Connection, entity_id: int, end: date, weeks: int = WEEKS) -> list[int]:
    start = end - timedelta(days=7 * weeks - 1)
    counts = [0] * weeks
    for (d,) in db.execute("SELECT date FROM mentions WHERE entity_id = ? AND date BETWEEN ? AND ?",
                           (entity_id, start.isoformat(), end.isoformat())):
        idx = (date.fromisoformat(d) - start).days // 7
        counts[min(idx, weeks - 1)] += 1
    return counts


def watch_count(term: str, texts: list[str]) -> int:
    """Items naming the term as a whole word. Acronyms match case-sensitively ("SEC" is not "second")."""
    flags = 0 if term.isupper() else re.IGNORECASE
    pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", flags)
    return sum(1 for t in texts if pat.search(t))


def _votes(db: sqlite3.Connection) -> dict[str, int]:
    return dict(db.execute("SELECT item_id, vote FROM feedback").fetchall())


def build_snapshot(db: sqlite3.Connection, tax: Taxonomy, *, narrate: bool = True, complete=None,
                   theme_window: int = 14, reading_days: int = 7) -> dict:
    end = sg.as_of_default(db)
    ids_by_name = dict(db.execute("SELECT name, id FROM entities").fetchall())
    key_by_name = dict(db.execute("SELECT name, key FROM entities").fetchall())

    def ent_ref(name: str) -> dict:
        return {"name": name, "slug": entity_slug(key_by_name.get(name, name.lower()))}

    themes = []
    for t in detect_themes(db, theme_window, n=3, as_of=end):
        prose = narrate_theme(db, t, tax.lens, complete=complete) if narrate else \
            {"title": " · ".join(t.entities[:3]), "narrative": "", "so_what": "", "cited_ids": [], "model": None}
        themes.append({
            "kind": t.kind, "title": prose["title"], "narrative": prose["narrative"], "so_what": prose["so_what"],
            "model": prose.get("model"), "entities": [ent_ref(e) for e in t.entities], "item_count": len(t.item_ids),
            "families": t.families, "confidence": t.confidence,
            "items": [{k: i[k] for k in ("id", "date", "source", "title", "url", "summary")} for i in t.items[:6]],
        })

    votes = _votes(db)
    reading = [dict(r, vote=votes.get(r["id"], 0))
               for r in sg.reading_list(db, days=reading_days, limit=30, as_of=end) if votes.get(r["id"], 0) >= 0][:8]
    summaries = dict(db.execute(f"SELECT id, summary FROM items WHERE id IN ({','.join('?' * len(reading))})",
                                [r["id"] for r in reading]).fetchall()) if reading else {}
    for r in reading:
        r["summary"] = summaries.get(r["id"], "")
        r["entities"] = [ent_ref(e) for e in r["entities"][:4]]

    def mover_rows(direction: str, limit: int) -> list[dict]:
        rows = []
        for m in sg.top_movers(db, theme_window, limit=limit, min_relevance=1 if direction == "up" else 0,
                               direction=direction, as_of=end):
            rows.append({**ent_ref(m.entity), "type": m.type, "current": m.current, "baseline": m.baseline,
                         "score": m.score, "families": m.families, "new": m.new, "confidence": m.confidence,
                         "series": weekly_series(db, ids_by_name[m.entity], end),
                         "evidence": m.evidence[:2]})
        return rows

    # Blind spots: share of lens categories, and full-text hits for the watchlist over 90 days.
    d28, d90 = (end - timedelta(days=27)).isoformat(), (end - timedelta(days=89)).isoformat()
    total28 = db.execute("SELECT COUNT(*) FROM items WHERE date >= ?", (d28,)).fetchone()[0] or 1
    cats = {c: n for c, n in db.execute("""SELECT category, COUNT(*) FROM items WHERE date >= ? AND extracted = 1
        GROUP BY category""", (d28,))}
    lens_cats = [{"category": c, "label": tax.categories.get(c, c).split(":")[0], "items": cats.get(c, 0),
                  "share": round(cats.get(c, 0) / total28, 3)} for c in tax.lens_categories]
    texts = [f"{t} {s or ''}" for t, s in db.execute("SELECT title, summary FROM items WHERE date >= ?", (d90,))]
    watch = [{"term": term, "mentions_90d": watch_count(term, texts)} for term in tax.watchlist]
    market = [{"category": c, "label": "Other (outside the market map)" if c == "other" else tax.categories.get(c, c).split(":")[0], "items": n,
               "share": round(n / total28, 3)} for c, n in sorted(cats.items(), key=lambda kv: -kv[1]) if c]

    counts = db.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM items").fetchone()
    sources = [{"source": s, "items": n} for s, n in db.execute(
        "SELECT source, COUNT(*) FROM items GROUP BY source ORDER BY 2 DESC")]
    models = sorted({t["model"] for t in themes if t.get("model")})
    return {
        "meta": {"as_of": end.isoformat(), "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                 "items": counts[0], "first_date": counts[1], "last_date": counts[2],
                 "entities": db.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                 "theme_window": theme_window, "reading_days": reading_days, "sources": sources,
                 "writer_models": models, "lens": tax.lens},
        "themes": themes,
        "reading": reading,
        "movers": {"up": mover_rows("up", 10), "down": mover_rows("down", 6)},
        "blind_spots": {"categories": lens_cats, "watchlist": watch, "total_28d": total28},
        "market": market,
    }


def entity_index(db: sqlite3.Connection, min_mentions: int = 3) -> list[dict]:
    return [{"name": n, "slug": entity_slug(k), "type": t, "mentions": m} for n, k, t, m in db.execute(
        "SELECT name, key, type, mentions FROM entities WHERE mentions >= ? ORDER BY mentions DESC", (min_mentions,))]


def entity_detail(db: sqlite3.Connection, name: str, days: int = 90, neighbours: int = 14, n_items: int = 10) -> dict:
    """One entity's neighbourhood for the explorer: co-mentioned entities, its items, validated relations."""
    eid, key, etype, first_seen, total = db.execute(
        "SELECT id, key, type, first_seen, mentions FROM entities WHERE name = ?", (name,)).fetchone()
    end = sg.as_of_default(db)
    start = (end - timedelta(days=days - 1)).isoformat()
    nb = db.execute("""SELECT e.name, e.key, e.type, COUNT(*) c FROM mentions a JOIN mentions b
        ON a.item_id = b.item_id AND b.entity_id != a.entity_id JOIN entities e ON e.id = b.entity_id
        WHERE a.entity_id = ? AND a.date >= ? GROUP BY e.id ORDER BY c DESC, e.mentions DESC LIMIT ?""",
                    (eid, start, neighbours)).fetchall()
    nb_keys = {r[1] for r in nb}
    items = db.execute("""SELECT i.id, i.date, i.source, i.title, i.url, i.relevance, i.category FROM mentions m
        JOIN items i ON i.id = m.item_id WHERE m.entity_id = ? AND m.date >= ?
        ORDER BY COALESCE(i.relevance,0) DESC, i.sources_count DESC, i.date DESC LIMIT ?""", (eid, start, n_items)).fetchall()
    item_rows = []
    for r in items:
        links = [entity_slug(k) for (k,) in db.execute(
            "SELECT e.key FROM mentions m JOIN entities e ON e.id = m.entity_id WHERE m.item_id = ?", (r[0],)) if k in nb_keys]
        item_rows.append({"id": r[0], "date": r[1], "source": r[2], "title": r[3], "url": r[4], "relevance": r[5],
                          "category": r[6], "links": links})
    rels = db.execute("""SELECT s.name, r.relation, t.name, COUNT(*) FROM relations r JOIN entities s ON s.id = r.src_id
        JOIN entities t ON t.id = r.dst_id WHERE r.src_id = ? OR r.dst_id = ? GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 8""",
                      (eid, eid)).fetchall()
    return {"name": name, "slug": entity_slug(key), "type": etype, "first_seen": first_seen, "mentions": total,
            "series": weekly_series(db, eid, end), "as_of": end.isoformat(), "window_days": days,
            "neighbours": [{"name": n, "slug": entity_slug(k), "type": t, "weight": c} for n, k, t, c in nb],
            "items": item_rows,
            "relations": [{"source": s, "relation": r, "target": t, "count": c} for s, r, t, c in rels]}


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
