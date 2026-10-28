"""Theme detection: groups of entities and items that moved together in a window. Deterministic, no LLM.

A theme starts from a seed entity, grows by the
entities most co-mentioned with it in the window, and owns the items that mention the seed or at least two of
its companions. Seeds whose items mostly belong to an earlier theme are skipped, so three themes are three
different stories. An LLM may later name and narrate a theme, but it never decides what the themes are.

Seeds come from two directions, because momentum alone misses what matters to the reader:
- momentum: the entity's mover score (rising against its baseline)
- lens: how many of the entity's items in the window the extraction rated relevant (2-3)
weight = (max(mover score, 0) + 1) * (0.5 + avg relevance) * (1.2 if both source families) + 1.5 * relevant items
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..signals import _confidence, as_of_default, top_movers


@dataclass
class Theme:
    kind: str  # "lens": built only from items rated relevant to the reader; "momentum": rising entities
    seed: str
    entities: list[str]
    item_ids: list[str]
    score: float
    families: int
    avg_relevance: float
    confidence: str
    items: list[dict] = field(default_factory=list)


def detect_themes(db: sqlite3.Connection, window_days: int = 14, n: int = 3, as_of: date | None = None,
                  min_items: int = 3, lens_first: bool = True) -> list[Theme]:
    """Up to n themes: first a lens theme (if there are enough relevant items), then momentum themes."""
    end = as_of or as_of_default(db)
    start = end - timedelta(days=window_days - 1)
    span = (start.isoformat(), end.isoformat())
    mover_score = {m.entity: m.score for m in top_movers(db, window_days, limit=500, min_current=2, as_of=end)}
    ids = dict(db.execute("SELECT name, id FROM entities").fetchall())
    names = {v: k for k, v in ids.items()}

    def items_of(eid: int) -> set[str]:
        return {r[0] for r in db.execute("SELECT item_id FROM mentions WHERE entity_id = ? AND date BETWEEN ? AND ?",
                                         (eid, *span))}

    rel = dict(db.execute("SELECT id, COALESCE(relevance, 0) FROM items WHERE date BETWEEN ? AND ?", span).fetchall())
    candidates = db.execute("""SELECT e.name, COUNT(*), COUNT(DISTINCT m.family) FROM mentions m
        JOIN entities e ON e.id = m.entity_id WHERE m.date BETWEEN ? AND ? GROUP BY e.id HAVING COUNT(*) >= 2""",
                            span).fetchall()
    seeds = []
    for name, cur, fams in candidates:
        its = items_of(ids[name])
        avg = sum(rel.get(i, 0) for i in its) / len(its)
        relevant = sum(1 for i in its if rel.get(i, 0) >= 2)
        if name not in mover_score and relevant < 2:
            continue
        weight = (max(mover_score.get(name, 0), 0) + 1) * (0.5 + avg) * (1.2 if fams > 1 else 1.0) + 1.5 * relevant
        seeds.append((weight, name, its))
    seeds.sort(key=lambda t: t[0], reverse=True)

    themes: list[Theme] = []
    claimed: set[str] = set()

    def finish(kind: str, seed: str, comp: list[str], items: set[str], weight: float) -> None:
        fams = db.execute(f"SELECT COUNT(DISTINCT family) FROM items WHERE id IN ({','.join('?' * len(items))})",
                          list(items)).fetchone()[0]
        avg_rel = sum(rel.get(i, 0) for i in items) / len(items)
        themes.append(Theme(kind=kind, seed=seed, entities=[seed, *comp], item_ids=sorted(items), score=round(weight, 2),
                            families=fams, avg_relevance=round(avg_rel, 2), confidence=_confidence(len(items), fams)))
        claimed.update(items)

    if lens_first:
        pool = {i for i, r in rel.items() if r >= 2}
        by_entity: dict[str, set[str]] = {}
        for name, iid in db.execute(f"""SELECT e.name, m.item_id FROM mentions m JOIN entities e ON e.id = m.entity_id
                WHERE m.item_id IN ({','.join('?' * len(pool))})""", list(pool)) if pool else []:
            by_entity.setdefault(name, set()).add(iid)
        ranked = sorted(by_entity.items(), key=lambda kv: (-sum(rel[i] for i in kv[1]), -len(kv[1]), kv[0]))
        if ranked and len(ranked[0][1]) >= 2:
            seed, seed_items = ranked[0]
            comp = [n for n, its in ranked[1:] if len(its & seed_items) >= 1][:4]
            items = seed_items | {i for n in comp for i in by_entity[n]}
            if len(items) >= min_items:
                finish("lens", seed, comp, items, float(len(items)))

    for weight, seed, seed_items in seeds:
        if len(themes) == n:
            break
        if any(seed in t.entities for t in themes):
            continue
        companions = db.execute("""SELECT b.entity_id, COUNT(*) c FROM mentions a JOIN mentions b
            ON a.item_id = b.item_id AND b.entity_id != a.entity_id
            WHERE a.entity_id = ? AND a.date BETWEEN ? AND ? GROUP BY b.entity_id HAVING c >= 2
            ORDER BY c DESC LIMIT 4""", (ids[seed], *span)).fetchall()
        comp = [names[c[0]] for c in companions]
        member: dict[str, int] = {}
        for c in comp:
            for i in items_of(ids[c]):
                member[i] = member.get(i, 0) + 1
        items = seed_items | {i for i, k in member.items() if k >= 2}
        if len(items) < min_items or len(items & claimed) > 0.5 * len(items):
            continue
        finish("momentum", seed, comp, items, weight)
    for t in themes:
        rows = db.execute(f"""SELECT id, date, source, title, url, relevance, summary FROM items
            WHERE id IN ({','.join('?' * len(t.item_ids))}) ORDER BY COALESCE(relevance,0) DESC, sources_count DESC, date DESC""",
                          t.item_ids).fetchall()
        t.items = [{"id": r[0], "date": r[1], "source": r[2], "title": r[3], "url": r[4], "relevance": r[5],
                    "summary": r[6]} for r in rows]
    return themes


