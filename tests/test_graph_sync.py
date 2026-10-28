"""ADR-0017: `graph sync` makes Neo4j an exact copy of graqle.json. Runs against an in-memory fake database."""

import json

import pytest

from signal_radar import graph_sync as gs
from signal_radar.config import GraphConfig

pytest.importorskip("graqle")


def graph_file(tmp_path, docs=("d1", "d2"), ents=("ent:a", "ent:b"), name="graqle.json"):
    """A small graph in graqle's networkx node-link format, like data/graqle/graqle.json."""
    nodes = [{"id": d, "label": d, "type": "DOCUMENT", "description": f"doc {d}", "url": f"https://x/{d}",
              "date": "2026-10-20", "tags": ["ai"], "chunks": [{"text": f"text of {d}", "type": "text"}]}
             for d in docs]
    nodes += [{"id": e, "label": e[4:], "type": "COMPANY", "description": e} for e in ents]
    links = [{"source": d, "target": e, "relationship": "MENTIONS", "weight": 1.0, "key": f"{d}___MENTIONS___{e}"}
             for d in docs for e in ents]
    if len(ents) > 1:
        links.append({"source": ents[0], "target": ents[1], "relationship": "COMPETES_WITH", "weight": 1.0,
                      "key": "x"})
    p = tmp_path / name
    p.write_text(json.dumps({"directed": True, "multigraph": True, "graph": {}, "nodes": nodes, "links": links}))
    return p


class FakeNeo4j:
    """Holds what Neo4j would hold after Neo4jConnector.save / save_chunks, keyed the same way."""

    def __init__(self):
        self.nodes, self.edges, self.chunks = {}, {}, {}
        self.upserts = 0

    def existing(self):
        return set(self.nodes), set(self.edges), set(self.chunks)

    def upsert(self, nodes, edges, chunks):
        self.upserts += 1
        self.nodes.update(nodes)
        for eid, e in edges.items():
            assert e["source"] in self.nodes and e["target"] in self.nodes, "edges are written after their nodes"
            self.edges[eid] = e
        for nid, cs in chunks.items():
            for i, c in enumerate(cs):
                if c.get("text"):
                    self.chunks[f"{nid}_chunk_{i}"] = c

    def delete(self, node_ids, edge_ids, chunk_ids):
        for i in edge_ids:
            self.edges.pop(i)
        for i in chunk_ids:
            self.chunks.pop(i)
        for i in node_ids:
            self.nodes.pop(i)
            self.edges = {k: e for k, e in self.edges.items() if i not in (e["source"], e["target"])}

    def counts(self):
        types = {}
        for e in self.edges.values():
            types[e["relationship"]] = types.get(e["relationship"], 0) + 1
        return {"nodes": len(self.nodes), "chunks": len(self.chunks), "edge_types": dict(sorted(types.items()))}

    def close(self):
        pass


def cfg(**sync):
    return GraphConfig.model_validate({
        "connector": "neo4j",
        "neo4j": {"database": "signalradar_test", "username": "u", "password_env": "X"},
        "sync": {"batch_size": 2, **sync}})


def test_shared_default_databases_are_refused():
    for db in ("neo4j", "system", "NEO4J"):
        with pytest.raises(ValueError, match="shared default"):
            GraphConfig.model_validate({"connector": "neo4j",
                                        "neo4j": {"database": db, "username": "u", "password_env": "X"}})


def test_projection_matches_what_graq_writes(tmp_path):
    proj = gs.project(graph_file(tmp_path))
    assert set(proj.nodes) == {"d1", "d2", "ent:a", "ent:b"}
    assert proj.nodes["d1"]["properties"]["url"] == "https://x/d1"   # the import dropped these; the sync keeps them
    assert proj.chunk_ids == {"d1_chunk_0", "d2_chunk_0"}
    assert proj.edge_types() == {"MENTIONS": 4, "COMPETES_WITH": 1}


def test_first_sync_fills_an_empty_database_and_verifies(tmp_path):
    db, out = FakeNeo4j(), tmp_path / "result.json"
    res = gs.sync(cfg(), target=db, graph_file=graph_file(tmp_path), result_file=out)
    assert res.ok and res.error is None
    assert res.add == {"nodes": 4, "edges": 5, "chunks": 2} and res.delete == {"nodes": 0, "edges": 0, "chunks": 0}
    assert res.actual == res.expected
    assert json.loads(out.read_text())["ok"] is True


def test_dry_run_reports_the_plan_and_writes_nothing(tmp_path):
    db, out = FakeNeo4j(), tmp_path / "result.json"
    res = gs.sync(cfg(), dry_run=True, target=db, graph_file=graph_file(tmp_path), result_file=out)
    assert res.ok and res.add["nodes"] == 4
    assert db.upserts == 0 and not db.nodes and not out.exists()


def test_stale_nodes_edges_and_chunks_are_deleted(tmp_path):
    """The daily build starts from scratch; whatever left the file must leave Neo4j too."""
    db = FakeNeo4j()
    gs.sync(cfg(max_node_delete_fraction=1), target=db, graph_file=graph_file(tmp_path, docs=("d1", "d2", "d3", "d4", "d5")),
            result_file=None)
    res = gs.sync(cfg(), target=db, graph_file=graph_file(tmp_path, docs=("d1", "d2", "d3", "d4"), name="g2.json"),
                  result_file=None)
    assert res.delete == {"nodes": 1, "edges": 2, "chunks": 1}
    assert "d5" not in db.nodes and "d5_chunk_0" not in db.chunks
    assert not any("d5" in (e["source"], e["target"]) for e in db.edges.values())
    assert res.ok


def test_edge_ids_are_stable_so_unchanged_edges_are_left_alone(tmp_path):
    """Edges are keyed by the file's networkx key, not graq's position counter: removing one document must not
    renumber the edges after it."""
    a = gs.project(graph_file(tmp_path, docs=("d1", "d2", "d3")))
    b = gs.project(graph_file(tmp_path, docs=("d1", "d3"), name="g2.json"))
    assert set(a.edges) - set(b.edges) == {"d2___MENTIONS___ent:a", "d2___MENTIONS___ent:b"}
    assert set(b.edges) <= set(a.edges)


def test_edges_under_other_ids_are_replaced_not_duplicated(tmp_path):
    db = FakeNeo4j()
    db.nodes = {n: {} for n in ("d1", "d2", "ent:a", "ent:b")}
    db.edges = {"e_d1_ent:a_0": {"source": "d1", "target": "ent:a", "relationship": "MENTIONS"}}
    res = gs.sync(cfg(), target=db, graph_file=graph_file(tmp_path), result_file=None)
    assert res.delete["edges"] == 1 and "e_d1_ent:a_0" not in db.edges
    assert db.counts()["edge_types"] == {"COMPETES_WITH": 1, "MENTIONS": 4}


def test_a_broken_build_cannot_empty_the_database(tmp_path):
    db = FakeNeo4j()
    gs.sync(cfg(), target=db, graph_file=graph_file(tmp_path, docs=tuple(f"d{i}" for i in range(10))), result_file=None)
    with pytest.raises(gs.SyncError, match="is the graph build broken"):
        gs.sync(cfg(), target=db, graph_file=graph_file(tmp_path, docs=("d1",), name="small.json"), result_file=None)
    assert len([n for n in db.nodes if n.startswith("d")]) == 10   # nothing deleted

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"directed": True, "multigraph": True, "graph": {}, "nodes": [], "links": []}))
    with pytest.raises(gs.SyncError, match="no nodes"):
        gs.sync(cfg(), target=db, graph_file=empty, result_file=None)


def test_a_mismatch_after_writing_fails_loudly(tmp_path):
    class Lossy(FakeNeo4j):
        def counts(self):
            c = super().counts()
            c["nodes"] -= 1
            return c

    out = tmp_path / "result.json"
    with pytest.raises(gs.SyncError, match="does not match"):
        gs.sync(cfg(), target=Lossy(), graph_file=graph_file(tmp_path), result_file=out)
    saved = json.loads(out.read_text())
    assert saved["ok"] is False and "does not match" in saved["error"]


def test_timeout_stops_between_batches_and_the_next_run_converges(tmp_path, monkeypatch):
    db, f = FakeNeo4j(), graph_file(tmp_path)
    clock = iter([0, 0] + [10_000] * 50)                       # start, first batch in time, then past the deadline
    monkeypatch.setattr(gs.time, "monotonic", lambda: next(clock))
    with pytest.raises(gs.SyncError, match="timeout"):
        gs.sync(cfg(timeout_seconds=10), target=db, graph_file=f, result_file=None)
    monkeypatch.undo()
    res = gs.sync(cfg(), target=db, graph_file=f, result_file=None)
    assert res.ok and res.actual == res.expected


def test_password_comes_from_its_own_variable_not_the_admin_one(monkeypatch):
    c = cfg()
    monkeypatch.setenv("NEO4J_PASSWORD", "admin-secret")
    monkeypatch.delenv("X", raising=False)
    monkeypatch.setattr("signal_radar.llm._keychain", lambda name: None)
    with pytest.raises(gs.SyncError, match="no Neo4j password: set X"):
        gs.neo4j_password(c)
    monkeypatch.setenv("X", "sr-secret")
    assert gs.neo4j_password(c) == "sr-secret"


def test_daily_step_runs_only_for_neo4j_and_only_after_a_good_build(monkeypatch):
    from signal_radar import cli

    calls = []
    monkeypatch.setattr("signal_radar.graph_sync.sync", lambda c, log: calls.append(c) or gs.SyncResult(
        "signalradar_test", False, "", {}, add={"nodes": 1}, delete={"nodes": 0}, ok=True))
    monkeypatch.setattr("signal_radar.config.load_graph", lambda: GraphConfig())          # networkx
    assert "skipped" in cli._daily_graph_sync(built=True) and not calls
    monkeypatch.setattr("signal_radar.config.load_graph", lambda: cfg(daily=True))
    assert "build failed" in cli._daily_graph_sync(built=False)["skipped"] and not calls  # Neo4j keeps its copy
    assert cli._daily_graph_sync(built=True)["add"] == {"nodes": 1} and len(calls) == 1

    def boom(c, log):
        raise gs.SyncError("Neo4j is down")
    monkeypatch.setattr("signal_radar.graph_sync.sync", boom)
    assert cli._daily_graph_sync(built=True) == {"error": "Neo4j is down"}               # reported, run continues
