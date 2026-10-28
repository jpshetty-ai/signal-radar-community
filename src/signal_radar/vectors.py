"""Vector search over the news corpus in Neo4j (ADR-0019).

How it fits together (read top to bottom):

1. Targets. The graph file (data/graqle/graqle.json, the source of truth, ADR-0017) holds a text chunk per node.
   We embed one chunk per news item (the DOCUMENT node whose id is the 16-hex item id) and the chunk of every
   entity (company, product, concept...). Section and document-path chunks repeat the item text, so they get no
   vector: a vector index only contains nodes that have the indexed property, which keeps three copies of one
   story out of the results.
2. Embedding. Texts go through the router's `embedding` role (llm.py, models.yaml), in batches, into a local
   cache keyed by hash(model + text). Unchanged text is never embedded twice; changing the model re-embeds all.
   The run is bounded (--max-chunks, --max-minutes) and Ctrl+C keeps every finished batch.
3. Neo4j. Our own HNSW vector index `signalradar_chunk_vec` on (:Chunk).vec, cosine, sized to the model's
   dimension. Vectors are written with db.create.setNodeVectorProperty (stored as float32), with `vec_key` (the
   cache key) so a stale vector is detected and rewritten. graqle's own 1024-dim index is left alone.
4. Search. The question is embedded with the model's query prefix, the index returns the nearest chunks
   (approximate k-nearest-neighbour), and one graph hop adds context: entities an item mentions, or recent items
   that mention an entity. That combination is GraphRAG: vectors find entry points, the graph explains them.
   Without Neo4j the same search runs as exact cosine over the local cache (numpy), so nothing depends on it.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .config import DATA_DIR

INDEX = "signalradar_chunk_vec"
STORE = DATA_DIR / "vectors.sqlite"
ITEM_ID = re.compile(r"^[0-9a-f]{16}$")
ENTITY_TYPES = {"COMPANY", "PRODUCT", "CONCEPT", "TECHNOLOGY", "PERSON", "INDUSTRY", "REGULATION", "SKILL"}

Log = Callable[[str], None]


@dataclass(frozen=True)
class Target:
    chunk_id: str  # Neo4j Chunk id: "<node id>_chunk_<n>", as graqle writes it
    node_id: str
    kind: str      # "item" | "entity"
    text: str


@dataclass
class Hit:
    id: str
    kind: str
    label: str
    score: float
    date: str | None = None
    url: str | None = None
    type: str | None = None
    source: str | None = None
    relevance: int | None = None
    context: list[str] = field(default_factory=list)  # one graph hop: mentioned entities / recent items


# ------------------------------------------------------------------ 1. targets


def targets_from_graph(graph_file: Path | None = None) -> list[Target]:
    from .config import PROJECT_ROOT, load_graph
    from .graph_sync import project

    proj = project(graph_file or PROJECT_ROOT / load_graph().file)
    out = []
    for nid, chunks in proj.chunks.items():
        ntype = proj.nodes[nid]["type"]
        kind = "item" if ITEM_ID.match(nid) else "entity" if ntype in ENTITY_TYPES else None
        if kind is None:
            continue
        for i, c in enumerate(chunks):
            text = (c.get("text", "") if isinstance(c, dict) else str(c)).strip()
            if text:
                out.append(Target(f"{nid}_chunk_{i}", nid, kind, text))
    return out


# ------------------------------------------------------------------ 2. embedding cache


def _key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()[:32]


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, model TEXT, dim INTEGER, vec BLOB, created_at TEXT)")
    # The last target list, so local (no-Neo4j) search can map vectors back to nodes without parsing the graph.
    db.execute("CREATE TABLE IF NOT EXISTS targets (chunk_id TEXT PRIMARY KEY, node_id TEXT, kind TEXT, key TEXT, label TEXT)")
    return db


@dataclass
class EmbedStats:
    model: str = ""
    dim: int = 0
    targets: int = 0
    unique_texts: int = 0
    cached: int = 0
    embedded: int = 0
    remaining: int = 0
    seconds: float = 0.0
    stopped: str | None = None  # "max_chunks" | "max_minutes" | "interrupted"


def embed_targets(targets: list[Target], router=None, db: sqlite3.Connection | None = None,
                  max_chunks: int | None = None, max_minutes: float | None = None, batch: int = 64,
                  log: Log = lambda m: None) -> EmbedStats:
    """Embed every target text not yet in the cache, within the budget. Safe to stop and re-run."""
    from .llm import LLMRouter

    router = router or LLMRouter.from_config(job="vectors")
    db = db or connect()
    model = router.spec("embedding").model
    st = EmbedStats(model=model, targets=len(targets))
    by_key = {_key(model, t.text): t.text for t in targets}
    st.unique_texts = len(by_key)
    have = {k for (k,) in db.execute("SELECT key FROM vectors WHERE model = ?", (model,))}
    todo = [k for k in by_key if k not in have]
    st.cached = st.unique_texts - len(todo)
    db.execute("DELETE FROM targets")
    db.executemany("INSERT INTO targets VALUES (?,?,?,?,?)",
                   [(t.chunk_id, t.node_id, t.kind, _key(model, t.text), t.text.split("\n", 1)[0][:120]) for t in targets])
    db.commit()
    t0 = time.monotonic()
    try:
        while st.embedded < len(todo):
            if max_chunks is not None and st.embedded >= max_chunks:
                st.stopped = "max_chunks"
                break
            if max_minutes is not None and time.monotonic() - t0 > max_minutes * 60:
                st.stopped = "max_minutes"
                break
            size = batch if max_chunks is None else min(batch, max_chunks - st.embedded)  # the cap is exact
            keys = todo[st.embedded:st.embedded + size]
            vecs = router.embed([by_key[k] for k in keys]).vectors
            now = datetime.now(UTC).isoformat(timespec="seconds")
            db.executemany("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?)",
                           [(k, model, len(v), np.asarray(v, dtype=np.float32).tobytes(), now) for k, v in zip(keys, vecs)])
            db.commit()  # every finished batch survives a stop
            st.embedded += len(keys)
            st.dim = len(vecs[0])
            if st.embedded // (batch * 10) != (st.embedded - len(keys)) // (batch * 10):
                log(f"embedded {st.embedded}/{len(todo)}")
    except KeyboardInterrupt:
        st.stopped = "interrupted"
    st.remaining = len(todo) - st.embedded
    st.seconds = round(time.monotonic() - t0, 1)
    if not st.dim:
        row = db.execute("SELECT dim FROM vectors WHERE model = ? LIMIT 1", (model,)).fetchone()
        st.dim = row[0] if row else 0
    return st


def _vectors_for(db: sqlite3.Connection, keys: list[str]) -> dict[str, np.ndarray]:
    out = {}
    for i in range(0, len(keys), 900):  # SQLite parameter limit
        part = keys[i:i + 900]
        q = f"SELECT key, vec FROM vectors WHERE key IN ({','.join('?' * len(part))})"
        out.update({k: np.frombuffer(b, dtype=np.float32) for k, b in db.execute(q, part)})
    return out


# ------------------------------------------------------------------ 3. Neo4j index and vectors


def ensure_index(session, dim: int) -> str:
    """Create our vector index, or recreate it when the model's dimension changed. Returns what happened."""
    row = session.run("SHOW VECTOR INDEXES YIELD name, options WHERE name = $n RETURN options", n=INDEX).single()
    if row is not None:
        if row["options"]["indexConfig"]["vector.dimensions"] == dim:
            return "exists"
        session.run(f"DROP INDEX {INDEX}")
    session.run(
        f"CREATE VECTOR INDEX {INDEX} IF NOT EXISTS FOR (c:Chunk) ON (c.vec) "
        "OPTIONS {indexConfig: {`vector.dimensions`: $dim, `vector.similarity_function`: 'cosine'}}", dim=dim)
    session.run("CALL db.awaitIndex($n, 600)", n=INDEX)
    return "created" if row is None else "recreated"


@dataclass
class PushStats:
    index: str = ""
    written: int = 0
    unchanged: int = 0
    missing_vector: int = 0   # targets whose text is not embedded yet (budget stop)
    missing_chunk: int = 0    # targets not in Neo4j yet (sync not run)
    cleared: int = 0          # chunks that had a vector but are no longer targets


def push_to_neo4j(target, db: sqlite3.Connection | None = None, batch: int = 500, log: Log = lambda m: None) -> PushStats:
    """Write cached vectors onto the Neo4j chunks that need them. `target` is a graph_sync.Neo4jTarget."""
    db = db or connect()
    rows = db.execute("SELECT chunk_id, kind, key FROM targets").fetchall()
    vecs = _vectors_for(db, sorted({k for _, _, k in rows}))
    st = PushStats()
    with target.driver.session(database=target.database) as s:
        if not vecs:
            return st
        st.index = ensure_index(s, len(next(iter(vecs.values()))))
        chunk_keys = {r["id"]: r["key"] for r in s.run("MATCH (c:Chunk) RETURN c.id AS id, c.vec_key AS key")}
        todo = []
        for cid, kind, key in rows:
            if cid not in chunk_keys:
                st.missing_chunk += 1
            elif key not in vecs:
                st.missing_vector += 1
            elif chunk_keys[cid] == key:
                st.unchanged += 1
            else:
                todo.append({"id": cid, "kind": kind, "key": key, "v": vecs[key].tolist()})
        for i in range(0, len(todo), batch):
            s.run("UNWIND $rows AS row MATCH (c:Chunk {id: row.id}) "
                  "CALL db.create.setNodeVectorProperty(c, 'vec', row.v) "
                  "SET c.vec_key = row.key, c.vec_kind = row.kind", rows=todo[i:i + batch])
            st.written += len(todo[i:i + batch])
            log(f"wrote {st.written}/{len(todo)} vectors")
        wanted = [cid for cid, _, _ in rows]
        st.cleared = s.run("MATCH (c:Chunk) WHERE c.vec IS NOT NULL AND NOT c.id IN $ids "
                           "REMOVE c.vec, c.vec_key, c.vec_kind RETURN count(c) AS n", ids=wanted).single()["n"]
    return st


# ------------------------------------------------------------------ 4. search


def _enrich(hits: list[Hit]) -> list[Hit]:
    """Source, relevance, title, date and URL for items, from the signal store (the graph lacks source and
    relevance)."""
    from .extract import connect as signals

    ids = [h.id for h in hits if h.kind == "item"]
    if not ids:
        return hits
    rows = signals().execute(
        f"SELECT id, source, relevance, title, date, url FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)
    info = {r[0]: r for r in rows}
    for h in hits:
        if h.id in info:
            _, h.source, h.relevance, title, date, url = info[h.id]
            h.label, h.date, h.url = title or h.label, h.date or date, h.url or url
    return hits


def search(query: str, k: int = 10, kind: str | None = None, router=None, target=None, expand: bool = True) -> list[Hit]:
    """Nearest chunks in the Neo4j vector index, plus one graph hop of context."""
    from .llm import LLMRouter

    router = router or LLMRouter.from_config(job="search")
    qv = router.embed([query], kind="query").vectors[0]
    with target.driver.session(database=target.database) as s:
        rows = s.run(
            "CALL db.index.vector.queryNodes($index, $n, $qv) YIELD node AS c, score "
            "WHERE $kind IS NULL OR c.vec_kind = $kind "
            "MATCH (c)<-[:HAS_CHUNK]-(n:CogniNode) "
            "RETURN n.id AS id, c.vec_kind AS kind, coalesce(n.title, n.label) AS label, n.entity_type AS type, "
            "       n.date AS date, n.url AS url, score ORDER BY score DESC LIMIT $k",
            index=INDEX, n=k * 4 if kind else k, qv=qv, kind=kind, k=k).data()
        hits = [Hit(r["id"], r["kind"], r["label"], round(r["score"], 4), r["date"], r["url"], r["type"]) for r in rows]
        if expand and hits:
            ctx = {r["id"]: r["ctx"] for r in s.run(
                "UNWIND $ids AS id MATCH (n:CogniNode {id: id}) "
                "OPTIONAL MATCH (n)-[:MENTIONS]->(e:CogniNode) WITH n, id, collect(DISTINCT e.label)[..8] AS ents "
                "OPTIONAL MATCH (d:CogniNode)-[:MENTIONS]->(n) WITH id, ents, d ORDER BY d.date DESC "
                "WITH id, ents, collect(coalesce(d.title, d.label))[..5] AS docs "
                "RETURN id, CASE WHEN size(ents) > 0 THEN ents ELSE docs END AS ctx",
                ids=[h.id for h in hits])}
            for h in hits:
                h.context = ctx.get(h.id, [])
    return _enrich(hits)


def search_local(query: str, k: int = 10, kind: str | None = None, router=None, db: sqlite3.Connection | None = None) -> list[Hit]:
    """Exact cosine over the local cache: the fallback when Neo4j is down, and a reference for the index."""
    from .llm import LLMRouter

    router = router or LLMRouter.from_config(job="search")
    db = db or connect()
    rows = [r for r in db.execute("SELECT chunk_id, node_id, kind, key, label FROM targets") if kind is None or r[2] == kind]
    vecs = _vectors_for(db, sorted({r[3] for r in rows}))
    rows = [r for r in rows if r[3] in vecs]
    if not rows:
        return []
    m = np.stack([vecs[r[3]] for r in rows])
    q = np.asarray(router.embed([query], kind="query").vectors[0], dtype=np.float32)
    scores = m @ q / (np.linalg.norm(m, axis=1) * np.linalg.norm(q) + 1e-12)
    best, seen = [], set()
    for i in np.argsort(-scores):
        if rows[i][1] in seen:
            continue
        seen.add(rows[i][1])
        best.append(Hit(rows[i][1], rows[i][2], rows[i][4], round(float(scores[i]), 4)))
        if len(best) == k:
            break
    return _enrich(best)


def hit_dict(h: Hit) -> dict:
    return {k: v for k, v in asdict(h).items() if v not in (None, [], "")}


def item_ranking(question: str, k: int = 30) -> list[str]:
    """Item ids nearest to a question, best first, for hybrid retrieval. Neo4j index first, the local cache if
    Neo4j is unreachable, [] if nothing is embedded yet. Never raises: Ask must work without vectors."""
    try:
        from .config import load_graph
        from .graph_sync import neo4j_target

        t = neo4j_target(load_graph())
        try:
            return [h.id for h in search(question, k=k, kind="item", target=t, expand=False)]
        finally:
            t.close()
    except Exception:  # noqa: BLE001 - fall back to the exact local search
        try:
            return [h.id for h in search_local(question, k=k, kind="item")]
        except Exception:  # noqa: BLE001
            return []


def rrf(rankings: list[list[str]], c: int = 60) -> dict[str, float]:
    """Reciprocal rank fusion: score = sum over rankings of 1 / (c + rank). Raw scores (BM25, cosine) live on
    different scales and cannot be added; ranks can. c = 60 is the usual constant: it damps the head of each list,
    so one method's #1 does not outweigh agreement between methods."""
    out: dict[str, float] = {}
    for ranking in rankings:
        for rank, iid in enumerate(ranking, 1):
            out[iid] = out.get(iid, 0.0) + 1.0 / (c + rank)
    return out

