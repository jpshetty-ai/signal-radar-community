"""ADR-0018: story clusters and lens links. A fake graph answers the module's Cypher from in-memory articles."""

import sqlite3
from datetime import date, timedelta
from itertools import combinations

import pytest

from signal_radar import brief as B
from signal_radar import graph_signals as gs

END = date(2026, 10, 21)
RECENT, OLD = END - timedelta(days=2), END - timedelta(days=20)


class FakeGraph:
    """Articles {id: (date, [entity names])}. Entity ids follow the graph: ent:<entity_key(name)>."""

    def __init__(self, docs: dict):
        self.docs = docs

    @staticmethod
    def eid(name):
        return gs.ENTITY + gs.entity_key(name)

    def __call__(self, cypher, **p):
        allowed = set(p.get("docs", self.docs))
        if cypher == gs.PAIRS:
            pairs = {}
            for d, (day, ents) in self.docs.items():
                if d not in allowed:
                    continue
                for a, b in combinations(sorted(set(ents), key=self.eid), 2):
                    r = pairs.setdefault((a, b), {"a": self.eid(a), "a_label": a, "b": self.eid(b), "b_label": b,
                                                  "recent": [], "before": 0})
                    if day >= p["start"]:
                        r["recent"].append(d)
                    else:
                        r["before"] += 1
            return [r for r in pairs.values() if len(r["recent"]) >= p["min_docs"]]
        if cypher == gs.LINKS:
            out = {}
            for d, (_, ents) in self.docs.items():
                if d not in allowed:
                    continue
                for x in ents:
                    for t in ents:
                        if x != t and self.eid(x) in p["ents"] and self.eid(t) in p["targets"]:
                            out.setdefault((x, t), {"x": self.eid(x), "t": self.eid(t), "target": t, "docs": []})["docs"].append(d)
            return list(out.values())
        if "RETURN t.id AS id" in cypher:
            names = {e for _, ents in self.docs.values() for e in ents}
            return [{"id": self.eid(n)} for n in names if self.eid(n) in p["ids"]]
        raise AssertionError(f"unexpected query {cypher[:60]}")


def store(docs: dict, families: dict | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript("""CREATE TABLE items (id TEXT, date TEXT, source TEXT, title TEXT, url TEXT, relevance INT,
        summary TEXT, sources_count INT, families TEXT); CREATE TABLE entities (name TEXT, key TEXT);""")
    for d, (day, ents) in docs.items():
        db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?)", (d, day, "tldr_ai", f"title {d}", f"https://x/{d}", 1, "",
                                                                     1, (families or {}).get(d, "tldr")))
    for n in {e for _, ents in docs.values() for e in ents}:
        db.execute("INSERT INTO entities VALUES (?,?)", (n, gs.entity_key(n)))
    return db


def doc(day, *ents):
    return (day.isoformat(), list(ents))


WORKSPACE = {  # one story: Claude moves into Google's office apps, in the same articles
    "w1": doc(RECENT, "Claude", "Google Slides", "Google Docs"),
    "w2": doc(RECENT, "Claude", "Google Slides", "Google Docs", "Google Sheets"),
    "w3": doc(RECENT, "Claude", "Google Slides", "Google Sheets", "Google Docs"),
    "w4": doc(RECENT, "Claude", "Google Sheets"),
}
HUB = {  # OpenAI in two unrelated stories: must stay two stories
    "h1": doc(RECENT, "OpenAI", "Jony Ive"), "h2": doc(RECENT, "OpenAI", "Jony Ive"), "h3": doc(RECENT, "OpenAI", "Jony Ive"),
    "h4": doc(RECENT, "OpenAI", "AMD"), "h5": doc(RECENT, "OpenAI", "AMD"), "h6": doc(RECENT, "OpenAI", "AMD"),
}


def test_one_story_from_links_that_share_articles():
    docs = {**WORKSPACE}
    clusters = gs.story_clusters(store(docs), FakeGraph(docs), END)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["anchor"] == "Claude" and set(c["entities"]) == {"Claude", "Google Slides", "Google Docs", "Google Sheets"}
    assert c["current"] == 4 and c["new"] is True
    assert gs.cluster_name(c["entities"]).startswith("Claude + ")


def test_a_hub_does_not_chain_unrelated_stories():
    docs = {**HUB}
    stories = {frozenset(c["entities"]) for c in gs.story_clusters(store(docs), FakeGraph(docs), END)}
    assert stories == {frozenset({"OpenAI", "AMD"}), frozenset({"OpenAI", "Jony Ive"})}


def test_steady_pairs_are_not_forming_but_sharp_rises_are():
    steady = {f"s{i}": doc(OLD if i < 12 else RECENT, "Nvidia", "TSMC") for i in range(15)}  # 12 before, 3 now
    rising = {f"r{i}": doc(OLD if i < 1 else RECENT, "Mistral", "ASML") for i in range(5)}    # 1 before, 4 now
    docs = {**steady, **rising}
    links = gs.forming_links(store(docs), FakeGraph(docs), END)
    assert [(lk["labels"][lk["a"]], lk["labels"][lk["b"]]) for lk in links] in ([("ASML", "Mistral")], [("Mistral", "ASML")])
    assert links[0]["new"] is False


def test_only_articles_in_the_given_store_count():
    """The public store has no excluded sources: Neo4j's copies of those articles must not make a public cluster."""
    docs = {**WORKSPACE}
    public = store({k: v for k, v in docs.items() if k != "w2"})          # w2 came from an excluded source
    clusters = gs.story_clusters(public, FakeGraph(docs), END)
    assert all("w2" not in c["doc_ids"] for c in clusters)


def test_lens_links_are_direct_co_mentions_through_aliases():
    docs = {"a": doc(RECENT, "AI agents", "Financial services"), "b": doc(RECENT, "AI agents", "Financial services"),
            "c": doc(RECENT, "OpenAI", "EU AI Act"), "d": doc(RECENT, "Claude", "Google Docs")}
    links = gs.lens_links(store(docs), FakeGraph(docs), ["AI agents", "OpenAI", "Claude", "EU AI Act"],
                          ["Financial Services", "EU AI Act"], END)   # target spelled differently: aliases apply
    assert [(x["target"], len(x["doc_ids"])) for x in links["AI agents"]] == [("Financial services", 2)]
    assert links["OpenAI"][0]["target"] == "EU AI Act"
    assert links["Claude"] == []                                          # no link: no lens "so what" allowed
    assert links["EU AI Act"][0]["self"] is True


@pytest.fixture
def brief_with_graph(monkeypatch):
    docs = {**WORKSPACE, "f1": doc(RECENT, "Google Docs", "Financial services")}
    graph = FakeGraph(docs)
    monkeypatch.setattr(gs, "neo4j_reader", lambda: (graph, lambda: None))
    return store(docs)


def test_brief_uses_stories_and_lens_links(brief_with_graph):
    signals = []
    graph = B._graph_signals(brief_with_graph, END, 7, lambda kind, **kw: signals.append({"sid": f"S{len(signals) + 1}",
                                                                                          "kind": kind, **kw}))
    assert graph["status"] == "neo4j"
    assert len(signals) == 1 and signals[0]["entities"][0] == "Claude"
    B._attach_lens(brief_with_graph, signals, graph, END)
    assert signals[0]["lens"][0]["target"] == "Financial services"       # through Google Docs, a cluster member
    assert "Financial services in 1 article(s)" in B._lens_line(signals[0])


def test_invented_lens_claims_are_dropped_and_linked_ones_kept():
    data = {"signals": [{"sid": "S1", "kind": "rising", "name": "Claude", "current": 5, "confidence": "medium", "evidence": [], "lens": []},
                        {"sid": "S2", "kind": "rising", "name": "AI agents", "current": 5, "confidence": "medium", "evidence": [],
                         "lens": [{"target": "Financial services", "self": False, "articles": 2, "evidence": []}]},
                        {"sid": "S3", "kind": "rising", "name": "Gemini", "current": 5, "confidence": "medium", "evidence": [], "lens": None}],
            "lens_items": [], "learning_candidates": [], "start": "", "end": ""}

    def complete(*a, **k):
        from signal_radar.llm import LLMResult
        return LLMResult(text="", data={"headline": "h", "learning": [], "opportunities": [], "signals": [
            {"sid": "S1", "so_what": "Banks will want this for compliance."},          # no link: invented
            {"sid": "S2", "so_what": "Banks are piloting agents for payments."},       # linked: allowed
            {"sid": "S3", "so_what": "A faster model for everyone."}]},                # graph down, no lens claim
            role="writing", provider="fake", model="m", input_tokens=0, output_tokens=0, cost_usd=0, latency_ms=0,
            stop_reason=None, input_hash="")

    p = B.write_prose(data, "lens", complete=complete)
    assert p["dropped_lens_claims"] == {"S1": "Banks will want this for compliance."}   # kept for audit
    assert set(p["so_what"]) == {"S2", "S3"}


def test_without_neo4j_the_brief_falls_back_and_checks_no_links():
    signals = [{"sid": "S1", "kind": "rising", "name": "X", "evidence": []}]
    graph = B._graph_signals(sqlite3.connect(":memory:"), END, 7, lambda *a, **k: None)   # conftest blocks Neo4j
    assert graph["clusters"] is None and graph["status"].startswith("unavailable")
    B._attach_lens(None, signals, graph, END)
    assert signals[0]["lens"] is None and "not checked" in B._lens_line(signals[0])


def test_public_brief_carries_no_newsletter_summary_anywhere(tmp_path, monkeypatch):
    """Lens links carry article evidence with summaries; the public edition must drop them wherever they sit."""
    from signal_radar.site import build

    brief = {"signals": [{"sid": "S1", "evidence": [{"id": "a", "summary": "newsletter text"}],
                          "lens": [{"target": "Financial services", "evidence": [{"id": "b", "summary": "newsletter text"}]}]}],
             "lens_items": [{"id": "c", "summary": "newsletter text"}], "prose": {"so_what": {"S1": "our own words"}}}
    monkeypatch.setattr("signal_radar.brief.latest_brief", lambda edition: brief)
    out = build._brief_for("public")
    assert "newsletter text" not in str(out) and "our own words" in str(out)
