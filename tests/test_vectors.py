"""Vector search (ADR-0019) without a model download or Neo4j: a fake router with deterministic vectors."""

import hashlib

import numpy as np
import pytest

from signal_radar import vectors as V


class FakeRouter:
    """Bag-of-words vectors: texts sharing words are close, so search behaviour is checkable."""

    DIM = 64

    def __init__(self):
        self.calls = []

    def spec(self, role):
        return type("S", (), {"model": "fake-embed"})()

    def _vec(self, text):
        v = np.zeros(self.DIM, dtype=np.float32)
        for w in text.lower().replace("?", "").split():
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.DIM] += 1
        return (v / (np.linalg.norm(v) or 1)).tolist()

    def embed(self, texts, **params):
        self.calls.append((list(texts), params))
        return type("E", (), {"vectors": [self._vec(t) for t in texts]})()


T = [V.Target("a" * 16 + "_chunk_0", "a" * 16, "item", "EU AI Office enforces general purpose AI rules"),
     V.Target("b" * 16 + "_chunk_0", "b" * 16, "item", "Nubank halts Monzo acquisition talks"),
     V.Target("ent_chunk_0", "ent", "entity", "EU AI Act: European regulation of AI systems"),
     V.Target("c" * 16 + "_chunk_0", "c" * 16, "item", "Nubank halts Monzo acquisition talks")]  # same text: one embedding


@pytest.fixture
def db(tmp_path):
    return V.connect(tmp_path / "v.sqlite")


def test_embeds_each_unique_text_once_and_caches(db):
    r = FakeRouter()
    st = V.embed_targets(T, router=r, db=db)
    assert (st.targets, st.unique_texts, st.embedded, st.remaining, st.dim) == (4, 3, 3, 0, 64)
    again = V.embed_targets(T, router=r, db=db)
    assert (again.cached, again.embedded) == (3, 0) and len(r.calls) == 1   # nothing re-embedded


def test_budget_is_exact_and_a_rerun_finishes_the_job(db):
    r = FakeRouter()
    st = V.embed_targets(T, router=r, db=db, max_chunks=2, batch=64)
    assert (st.embedded, st.remaining, st.stopped) == (2, 1, "max_chunks")
    st = V.embed_targets(T, router=r, db=db)
    assert (st.cached, st.embedded, st.remaining) == (2, 1, 0)


def test_local_search_ranks_by_meaning_and_returns_each_node_once(db, monkeypatch):
    monkeypatch.setattr(V, "_enrich", lambda hits: hits)  # no signal store in this test
    r = FakeRouter()
    V.embed_targets(T, router=r, db=db)
    hits = V.search_local("what is the EU AI Office doing", k=3, router=r, db=db)
    assert hits[0].id == "a" * 16 and hits[0].label.startswith("EU AI Office")
    assert len({h.id for h in hits}) == len(hits)
    assert r.calls[-1][1] == {"kind": "query"}                       # questions are embedded as queries
    assert [h.kind for h in V.search_local("EU AI Act", kind="entity", router=r, db=db)] == ["entity"]


def test_targets_skip_section_and_path_copies(tmp_path, monkeypatch):
    class Proj:
        nodes = {"a" * 16: {"type": "DOCUMENT"}, "sec::x::t": {"type": "SECTION"}, "doc::x": {"type": "Entity"},
                 "nvidia": {"type": "COMPANY"}}
        chunks = {k: [{"text": f"text of {k}"}] for k in nodes}

    import signal_radar.graph_sync as gs
    monkeypatch.setattr(gs, "project", lambda f: Proj)
    got = {(t.node_id, t.kind) for t in V.targets_from_graph(tmp_path / "g.json")}
    assert got == {("a" * 16, "item"), ("nvidia", "entity")}


def test_rrf_rewards_agreement_over_a_single_first_place():
    fused = V.rrf([["a", "b", "c"], ["b", "x", "a"]])
    assert max(fused, key=fused.get) == "b"            # 2nd + 1st beats 1st + 3rd
    assert fused["a"] > fused["x"] and "c" in fused     # an item from one list still counts


def test_ask_retrieval_fuses_keyword_and_vector_rankings(monkeypatch):
    """Hybrid Ask: an item only vector search finds (no shared keyword) enters the evidence."""
    import sqlite3

    from signal_radar import signals as sg
    from signal_radar.site import prose

    db = sqlite3.connect(":memory:")
    db.executescript(sg.SCHEMA)
    rows = [("k1", "Insurance broker startup raises funds", "Insurance paperwork platform."),
            ("v1", "Commission starts enforcing the AI Act", "The AI Office begins supervision.")]
    for iid, title, summary in rows:
        db.execute("INSERT INTO items (id, date, source, title, url, relevance, sources_count, summary) VALUES "
                   "(?, '2026-10-01', 'tldr_ai', ?, 'https://x', 1, 1, ?)", (iid, title, summary))
        db.execute("INSERT INTO items_fts VALUES (?, ?, ?)", (iid, title, summary))
    q = "What is happening with insurance rules?"
    assert [i["id"] for i in prose.retrieve(db, q, vector=False)["items"]] == ["k1"]
    monkeypatch.setattr(V, "item_ranking", lambda question, k=30: ["v1", "k1"])
    ev = prose.retrieve(db, q)
    assert ev["vector"] and [i["id"] for i in ev["items"]] == ["k1", "v1"]   # k1 ranked by both lists, v1 by one
