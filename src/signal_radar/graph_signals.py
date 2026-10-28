"""Signals that need the graph's structure, read from Neo4j (ADR-0018).

- story_clusters: entity pairs mentioned together for the first time, or far more often than before, grouped into
  stories. Two forming links belong to one story when they share an entity *and* share articles, so a hub such as
  OpenAI (in many unrelated stories) does not chain them into one blob.
- lens_links: articles that mention a signal's entity together with one of the reader's lens targets. The brief
  allows a reader-specific "so what" only where such a link exists.

Neo4j holds every source; the SQLite store the caller passes decides which articles count (the public store has
no excluded sources), so a public signal never rests on an excluded source. Article metadata (title, source
family) comes from that store too. Counting rules are deterministic; no model involved.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta

from . import signals as sg
from .signals import entity_key, load_aliases

Run = Callable[..., list[dict]]   # run(cypher, **params) -> rows; a read-only Neo4j session, or a fake in tests

ENTITY = "ent:"   # entity node ids in the graph ("ent:<key>", graph_export.batch_payload)


def neo4j_reader() -> tuple[Run, Callable[[], None]]:
    """A read-only `run` on the configured database, as the restricted user; and a close function."""
    from neo4j import GraphDatabase

    from .config import load_graph
    from .graph_sync import SyncError, neo4j_password

    cfg = load_graph()
    if cfg.connector != "neo4j" or cfg.neo4j is None:
        raise SyncError(f"graph served from {cfg.connector}, not neo4j (config/graph.yaml)")
    n = cfg.neo4j
    driver = GraphDatabase.driver(n.uri, auth=(n.username, neo4j_password(cfg)))

    def run(cypher: str, **params) -> list[dict]:
        with driver.session(database=n.database, default_access_mode="READ") as s:
            return s.run(cypher, **params).data()

    return run, driver.close


def _item_ids(db: sqlite3.Connection, start: date, end: date) -> list[str]:
    return [r[0] for r in db.execute("SELECT id FROM items WHERE date BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))]


def _families(db: sqlite3.Connection, ids: set[str]) -> int:
    fams = set()
    for chunk in [list(ids)[i:i + 500] for i in range(0, len(ids), 500)]:
        q = f"SELECT families FROM items WHERE id IN ({','.join('?' * len(chunk))})"
        for (f,) in db.execute(q, chunk):
            fams.update((f or "").split(","))
    return len(fams - {""})


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


# ------------------------------------------------------------------------------------------------ story clusters

PAIRS = """
MATCH (a:CogniNode)<-[:MENTIONS]-(d:CogniNode)-[:MENTIONS]->(b:CogniNode)
WHERE d.id IN $docs AND a.id STARTS WITH $ent AND b.id STARTS WITH $ent AND a.id < b.id
WITH a, b, collect(CASE WHEN d.date >= $start THEN d.id END) AS recent,
     count(CASE WHEN d.date < $start THEN 1 END) AS before
WHERE size(recent) >= $min_docs
RETURN a.id AS a, a.label AS a_label, b.id AS b, b.label AS b_label, recent, before
"""


def forming_links(db: sqlite3.Connection, run: Run, end: date, days: int = 7, periods: int = 4,
                  min_docs: int = 3, ratio: float = 2.0) -> list[dict]:
    """Pairs in at least `min_docs` articles this window and at least `ratio` times their average earlier window."""
    start = end - timedelta(days=days - 1)
    base_start = start - timedelta(days=days * periods)
    rows = run(PAIRS, docs=_item_ids(db, base_start, end), ent=ENTITY, start=start.isoformat(), min_docs=min_docs)
    out = []
    for r in rows:
        recent, base_mean = set(r["recent"]), r["before"] / periods
        if r["before"] == 0 or len(recent) >= ratio * base_mean:
            out.append({"a": r["a"], "b": r["b"], "labels": {r["a"]: r["a_label"], r["b"]: r["b_label"]},
                        "docs": recent, "baseline": round(base_mean, 2), "new": r["before"] == 0})
    return out


def story_clusters(db: sqlite3.Connection, run: Run, end: date, days: int = 7, periods: int = 4, min_docs: int = 3,
                   ratio: float = 2.0, overlap: float = 0.3, limit: int = 4) -> list[dict]:
    links = forming_links(db, run, end, days, periods, min_docs, ratio)
    parent = list(range(len(links)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_entity: dict[str, list[int]] = defaultdict(list)
    for i, link in enumerate(links):
        by_entity[link["a"]].append(i)
        by_entity[link["b"]].append(i)
    for idx in by_entity.values():          # same entity AND same articles: one story
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                i, j = idx[x], idx[y]
                if _jaccard(links[i]["docs"], links[j]["docs"]) >= overlap:
                    parent[find(i)] = find(j)

    groups: dict[int, list[dict]] = defaultdict(list)
    for i, link in enumerate(links):
        groups[find(i)].append(link)
    clusters = []
    for members in groups.values():
        docs = set().union(*(m["docs"] for m in members))
        labels = {k: v for m in members for k, v in m["labels"].items()}
        degree = defaultdict(int)
        for m in members:
            degree[m["a"]] += len(m["docs"])
            degree[m["b"]] += len(m["docs"])
        order = sorted(labels, key=lambda k: (-degree[k], labels[k]))
        fams = _families(db, docs)
        clusters.append({
            "entities": [labels[k] for k in order], "anchor": labels[order[0]],
            "links": [{"pair": [m["labels"][m["a"]], m["labels"][m["b"]]], "current": len(m["docs"]),
                       "baseline": m["baseline"], "new": m["new"]} for m in members],
            "doc_ids": sorted(docs), "current": len(docs), "families": fams,
            "new": all(m["new"] for m in members), "confidence": sg._confidence(len(docs), fams),
        })
    clusters.sort(key=lambda c: (-c["current"], -len(c["entities"]), c["anchor"]))
    return clusters[:limit]


def cluster_name(entities: list[str], max_named: int = 4) -> str:
    head, rest = entities[0], entities[1:]
    named = ", ".join(rest[:max_named]) + (f" and {len(rest) - max_named} more" if len(rest) > max_named else "")
    return f"{head} + {named}"


# --------------------------------------------------------------------------------------------------- lens links

LINKS = """
MATCH (x:CogniNode)<-[:MENTIONS]-(d:CogniNode)-[:MENTIONS]->(t:CogniNode)
WHERE x.id IN $ents AND t.id IN $targets AND x <> t AND d.id IN $docs
RETURN x.id AS x, t.id AS t, t.label AS target, collect(DISTINCT d.id) AS docs
"""


def target_ids(names: list[str]) -> dict[str, str]:
    """Lens target names -> graph ids, through the alias table ("Financial Services" -> ent:financialservices)."""
    aliases = load_aliases()
    out = {}
    for n in names:
        k = entity_key(n)
        out[ENTITY + aliases.get(k, k)] = aliases.display.get(aliases.get(k, k), n)
    return out


def lens_links(db: sqlite3.Connection, run: Run, names: list[str], targets: list[str], end: date,
               lookback_days: int = 90) -> dict[str, list[dict]]:
    """{entity name: [{target, doc_ids}]} for entities that share an article with a lens target. Entities that are
    themselves targets link to themselves."""
    keys = dict(db.execute(f"SELECT name, key FROM entities WHERE name IN ({','.join('?' * len(names))})", names)) if names else {}
    tids = target_ids(targets)
    out: dict[str, list[dict]] = {n: [] for n in names}
    for n, k in keys.items():
        if ENTITY + k in tids:
            out[n].append({"target": tids[ENTITY + k], "doc_ids": [], "self": True})
    ents = {ENTITY + k: n for n, k in keys.items()}
    if not ents:
        return out
    rows = run(LINKS, ents=list(ents), targets=list(tids), ent=ENTITY,
               docs=_item_ids(db, end - timedelta(days=lookback_days - 1), end))
    for r in rows:
        out[ents[r["x"]]].append({"target": r["target"], "doc_ids": sorted(r["docs"], reverse=True)})
    for n in out:
        out[n].sort(key=lambda link: (not link.get("self"), -len(link["doc_ids"])))
    return out


def present_targets(run: Run, targets: list[str]) -> tuple[list[str], list[str]]:
    """Which lens targets exist in the graph, and which do not (yet): the honest coverage of the lens."""
    tids = target_ids(targets)
    found = {r["id"] for r in run("MATCH (t:CogniNode) WHERE t.id IN $ids RETURN t.id AS id", ids=list(tids))}
    return [tids[i] for i in tids if i in found], [tids[i] for i in tids if i not in found]
