"""Make the Neo4j database an exact copy of data/graqle/graqle.json (ADR-0017).

The file is the source of truth (graqle calls it Tier 0); Neo4j is a projection of it (Tier 1). graq itself can
only add to Neo4j, never remove, so a graph rebuilt from scratch every day would leave stale nodes behind. This
module projects the file the way graq's own `Graqle.to_neo4j` does (CogniNode nodes with their properties, typed
relationships, Chunk nodes), then deletes whatever the file no longer has, then checks that the database matches
the file exactly. It never calls `to_neo4j` itself, because that rewrites the JSON file first.

The sync is idempotent and convergent: if it stops halfway (timeout, lost connection), the next run finishes
the job. It only ever touches the one configured database, and refuses the shared defaults `neo4j` and `system`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .config import DATA_DIR, GraphConfig, load_graph

RESULT_FILE = DATA_DIR / "graqle" / "neo4j_sync.json"
CHUNK_LINK = "HAS_CHUNK"


class SyncError(RuntimeError):
    pass


# ------------------------------------------------------------------ what the file says Neo4j should hold


@dataclass
class Projection:
    nodes: dict[str, dict]                 # id -> {label, type, description, properties}, as Graqle.to_neo4j
    edges: dict[str, dict]                 # id -> {source, target, relationship, weight}
    chunks: dict[str, list[dict]]          # node id -> chunks, as Graqle.to_neo4j passes to save_chunks
    file_sha256: str = ""

    @property
    def chunk_ids(self) -> set[str]:
        # Same ids and the same skip rule as Neo4jConnector.save_chunks (empty text is not written).
        return {f"{nid}_chunk_{i}" for nid, cs in self.chunks.items() for i, c in enumerate(cs)
                if (c.get("text", "") if isinstance(c, dict) else str(c))}

    def edge_types(self) -> Counter:
        return Counter(e["relationship"] for e in self.edges.values())


def project(graph_file: Path) -> Projection:
    """Nodes through graq's own loader, so ids, types and properties are exactly what graq would write.

    Edges come from the file's networkx links, keyed by their `key` ("source___RELATION___target"). graq's loader
    numbers edges by position ("e_<source>_<target>_<n>"), so one removed document would renumber every later edge
    and the sync would rewrite thousands of unchanged edges each day. A diff needs ids that stay put.
    """
    from graqle.core.graph import Graqle

    logging.getLogger("graqle").setLevel(logging.ERROR)  # graq logs a line per load step
    raw = graph_file.read_bytes()
    g = Graqle.from_json(str(graph_file))
    nodes, chunks = {}, {}
    for nid, n in g.nodes.items():
        props = dict(n.properties)
        if props.get("chunks"):
            chunks[nid] = props["chunks"]
        nodes[nid] = {"label": n.label, "type": n.entity_type, "description": n.description, "properties": props}
    edges: dict[str, dict] = {}
    for link in json.loads(raw).get("links", []):
        s, t, rel = str(link["source"]), str(link["target"]), link.get("relationship", "RELATED_TO")
        eid = str(link.get("key") or f"{s}___{rel}___{t}")
        if eid in edges:
            raise SyncError(f"duplicate edge key {eid!r} in {graph_file}")
        edges[eid] = {"source": s, "target": t, "relationship": rel, "weight": link.get("weight", 1.0)}
    return Projection(nodes, edges, chunks, hashlib.sha256(raw).hexdigest())


# ------------------------------------------------------------------ the database side


class Target(Protocol):
    """What the sync needs from a graph database. Neo4jTarget below; tests use an in-memory fake."""

    def existing(self) -> tuple[set[str], set[str], set[str]]: ...           # node ids, edge ids, chunk ids
    def upsert(self, nodes: dict, edges: dict, chunks: dict) -> None: ...
    def delete(self, node_ids: set[str], edge_ids: set[str], chunk_ids: set[str]) -> None: ...
    def counts(self) -> dict: ...                                             # {"nodes", "chunks", "edge_types"}


class Neo4jTarget:
    def __init__(self, uri: str, username: str, password: str, database: str):
        from graqle.connectors.neo4j import Neo4jConnector
        from neo4j import GraphDatabase

        self.database = database
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self.connector = Neo4jConnector(uri=uri, username=username, password=password, database=database)

    def close(self) -> None:
        self.driver.close()
        self.connector.close()

    def _ids(self, query: str) -> set[str]:
        with self.driver.session(database=self.database) as s:
            return {r["id"] for r in s.run(query)}

    def existing(self) -> tuple[set[str], set[str], set[str]]:
        return (self._ids("MATCH (n:CogniNode) RETURN n.id AS id"),
                self._ids(f"MATCH (:CogniNode)-[r]->(:CogniNode) WHERE type(r) <> '{CHUNK_LINK}' RETURN r.id AS id"),
                self._ids("MATCH (c:Chunk) RETURN c.id AS id"))

    def upsert(self, nodes: dict, edges: dict, chunks: dict) -> None:
        self.connector.save(nodes, edges)
        if chunks:
            self.connector.save_chunks(chunks)

    def delete(self, node_ids: set[str], edge_ids: set[str], chunk_ids: set[str]) -> None:
        with self.driver.session(database=self.database) as s:
            if edge_ids:
                s.run(f"UNWIND $ids AS id MATCH (:CogniNode)-[r {{id: id}}]->(:CogniNode) "
                      f"WHERE type(r) <> '{CHUNK_LINK}' DELETE r", ids=list(edge_ids))
            if chunk_ids:
                s.run("UNWIND $ids AS id MATCH (c:Chunk {id: id}) DETACH DELETE c", ids=list(chunk_ids))
            if node_ids:
                s.run("UNWIND $ids AS id MATCH (n:CogniNode {id: id}) DETACH DELETE n", ids=list(node_ids))

    def counts(self) -> dict:
        with self.driver.session(database=self.database) as s:
            nodes = s.run("MATCH (n:CogniNode) RETURN count(n) AS c").single()["c"]
            chunks = s.run("MATCH (c:Chunk) RETURN count(c) AS c").single()["c"]
            types = {r["t"]: r["c"] for r in s.run(
                f"MATCH (:CogniNode)-[r]->(:CogniNode) WHERE type(r) <> '{CHUNK_LINK}' "
                "RETURN coalesce(r.relationship, type(r)) AS t, count(*) AS c")}
        return {"nodes": nodes, "chunks": chunks, "edge_types": types}


def neo4j_password(cfg: GraphConfig) -> str:
    """The Signal Radar user's password: its own env var, else the macOS keychain. Never the admin password."""
    from .llm import _keychain

    name = cfg.neo4j.password_env
    pw = os.environ.get(name) or _keychain(name)
    if not pw:
        raise SyncError(f"no Neo4j password: set {name} or store it in the keychain "
                        f"(security add-generic-password -s signal-radar -a {name} -w)")
    return pw


def neo4j_target(cfg: GraphConfig) -> Neo4jTarget:
    if cfg.neo4j is None:
        raise SyncError("config/graph.yaml has no neo4j: section")
    n = cfg.neo4j
    return Neo4jTarget(n.uri, n.username, neo4j_password(cfg), n.database)


# ------------------------------------------------------------------ plan, apply, verify


@dataclass
class SyncResult:
    database: str
    dry_run: bool
    file_sha256: str
    expected: dict
    add: dict = field(default_factory=dict)       # ids new to Neo4j
    delete: dict = field(default_factory=dict)    # ids Neo4j has and the file does not
    actual: dict = field(default_factory=dict)
    ok: bool = False
    seconds: float = 0.0
    finished_at: str = ""
    error: str | None = None


def _batches(d: dict, size: int):
    items = list(d.items())
    for i in range(0, len(items), size):
        yield dict(items[i:i + size])


def sync(cfg: GraphConfig | None = None, dry_run: bool = False, target: Target | None = None,
         graph_file: Path | None = None, result_file: Path | None = RESULT_FILE, log=lambda m: None) -> SyncResult:
    cfg = cfg or load_graph()
    if cfg.neo4j is None:
        raise SyncError("config/graph.yaml has no neo4j: section")
    t0 = time.monotonic()
    deadline = t0 + cfg.sync.timeout_seconds
    graph_file = graph_file or cfg.file_path
    proj = project(graph_file)
    if not proj.nodes:
        raise SyncError(f"{graph_file} has no nodes: refusing to empty the database")
    want_nodes, want_edges, want_chunks = set(proj.nodes), set(proj.edges), proj.chunk_ids
    res = SyncResult(cfg.neo4j.database, dry_run, proj.file_sha256,
                     {"nodes": len(want_nodes), "chunks": len(want_chunks),
                      "edge_types": dict(sorted(proj.edge_types().items()))})

    own = target is None
    target = target or neo4j_target(cfg)
    try:
        have_nodes, have_edges, have_chunks = target.existing()
        stale_nodes, stale_edges, stale_chunks = have_nodes - want_nodes, have_edges - want_edges, have_chunks - want_chunks
        res.add = {"nodes": len(want_nodes - have_nodes), "edges": len(want_edges - have_edges),
                   "chunks": len(want_chunks - have_chunks)}
        res.delete = {"nodes": len(stale_nodes), "edges": len(stale_edges), "chunks": len(stale_chunks)}
        log(f"plan for {cfg.neo4j.database}: add {res.add}, delete {res.delete}")
        if have_nodes and len(stale_nodes) > cfg.sync.max_node_delete_fraction * len(have_nodes):
            raise SyncError(f"would delete {len(stale_nodes)} of {len(have_nodes)} nodes (limit "
                            f"{cfg.sync.max_node_delete_fraction:.0%}): is the graph build broken?")
        if dry_run:
            res.ok = True
            return res

        # Upsert everything (properties may have changed on existing ids), nodes before the edges that need them.
        size = cfg.sync.batch_size
        for part in ("nodes", "edges", "chunks"):
            source = {"nodes": proj.nodes, "edges": proj.edges, "chunks": proj.chunks}[part]
            for batch in _batches(source, size):
                if time.monotonic() > deadline:
                    raise SyncError(f"timeout after {cfg.sync.timeout_seconds} s while writing {part}; "
                                    "the next sync completes the job")
                target.upsert(batch if part == "nodes" else {}, batch if part == "edges" else {},
                              batch if part == "chunks" else {})
            log(f"upserted {len(source)} {part}")
        target.delete(stale_nodes, stale_edges, stale_chunks)
        log(f"deleted {res.delete}")

        res.actual = target.counts()
        res.ok = res.actual == res.expected and target.existing() == (want_nodes, want_edges, want_chunks)
        if not res.ok:
            raise SyncError(f"Neo4j does not match the file after the sync: expected {res.expected}, got {res.actual}")
        return res
    except Exception as e:
        res.ok, res.error = False, str(e)
        raise
    finally:
        res.seconds = round(time.monotonic() - t0, 1)
        res.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if result_file is not None and not dry_run:
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text(json.dumps(asdict(res), indent=2))
        if own:
            target.close()
