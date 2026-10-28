from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from signal_radar import signals as sg
from signal_radar.corpus import Corpus
from signal_radar.extract import Taxonomy, connect, extract_items, schema, system_prompt
from signal_radar.items import Item, item_id

AS_OF = date(2026, 10, 2)


def mk(title, summary, day, source="tldr_ai", url=None):
    url = url or f"https://news.example.com/{abs(hash((title, day))) % 10**8}"
    return Item(id=item_id(url, title), source_id=source, date=day, url=url, title=title, summary=summary)


def ex(*ents, rels=(), category="agents_devtools", relevance=2):
    return {"category": category, "secondary_category": None, "relevance": relevance, "relevance_reason": "why",
            "entities": [{"name": n, "type": t} for n, t in ents], "relations": [
                {"source": s, "relation": r, "target": t} for s, r, t in rels]}


class FakeModel:
    """Returns a canned extraction per title; records calls."""

    def __init__(self, by_title):
        self.by_title, self.calls = by_title, []

    def __call__(self, role, prompt, system=None, json_schema=None):
        self.calls.append(prompt)
        title = prompt.splitlines()[0].lstrip("# ").strip()
        return SimpleNamespace(data=self.by_title[title], provider="fake", model="m", latency_ms=1)


@pytest.fixture
def store(tmp_path):
    """Corpus over 5 weeks: MCP rises sharply in the last week, Kubernetes fades, EU AI Act appears new."""
    corpus, db = Corpus(tmp_path / "corpus"), connect(tmp_path / "signals.sqlite")
    items, canned = [], {}

    def add(title, summary, day, extraction, source="tldr_ai", url=None):
        it = mk(title, summary, day, source, url)
        items.append(it)
        canned[title] = extraction

    for w in range(1, 5):  # baseline weeks: MCP once a week, Kubernetes 4 times a week
        day = AS_OF - timedelta(days=7 * w + 1)
        add(f"MCP note {w}", "Anthropic's MCP gets a spec update.", day,
            ex(("Model Context Protocol", "Technology"), ("Anthropic", "Company")))
        for k in range(4):
            add(f"K8s story {w}-{k}", "Kubernetes and Google news.", day - timedelta(days=k),
                ex(("Kubernetes", "Technology"), ("Google", "Company"), category="ai_infrastructure", relevance=1))
    for k in range(6):  # current week: MCP 6 times, from two independent sources
        add(f"MCP launch {k}", "MCP servers for banks: Anthropic and Stripe ship MCP support.", AS_OF - timedelta(days=k),
            ex(("MCP", "Technology"), ("Anthropic", "Company"), ("Stripe", "Company"), ("banking", "Industry"),
               rels=[("Anthropic", "RELEASED_BY", "MCP"), ("MCP", "ADOPTED_IN", "banking")]),
            source="alphasignal" if k % 2 else "tldr_ai")
    add("AI Act deadline", "The EU AI Act obligations start; the AI model rules apply.", AS_OF - timedelta(days=1),
        ex(("EU AI Act", "Regulation"), ("AI", "Technology"), ("Financial services", "Industry"),
           category="regulation_governance", relevance=3))
    corpus.write(items)
    model = FakeModel(canned)
    stats = extract_items(corpus=corpus, db=db, complete=model, taxonomy=Taxonomy.load())
    assert stats.done == len(items) and stats.failed == 0
    return corpus, db, model


def test_extraction_is_resumable_and_redo_forces(store):
    corpus, db, model = store
    n = len(model.calls)
    again = extract_items(corpus=corpus, db=db, complete=model, taxonomy=Taxonomy.load())
    assert again.skipped_current == n and again.done == 0 and len(model.calls) == n
    redo = extract_items(corpus=corpus, db=db, complete=model, taxonomy=Taxonomy.load(), redo=True, limit=2)
    assert redo.done == 2


def test_schema_and_prompt_follow_taxonomy():
    tax = Taxonomy.load()
    js = schema(tax)
    assert js["properties"]["category"]["enum"] == list(tax.categories)
    assert list(js["properties"])[:2] == ["category", "secondary_category"]  # classify before listing entities
    assert "regulation_governance" in system_prompt(tax) and "RELEASED_BY" in system_prompt(tax)


def test_rebuild_canonicalises_filters_and_fixes_relations(store):
    corpus, db, _ = store
    st = sg.rebuild(corpus, db)
    names = {r[0] for r in db.execute("SELECT name FROM entities")}
    assert "Model Context Protocol" in names and "MCP" not in names  # alias -> one entity, common spelling kept
    assert "AI" not in names                                           # generic, dropped
    fs_items = {r[0] for r in db.execute("""SELECT i.title FROM mentions m JOIN entities e ON e.id = m.entity_id
                                            JOIN items i ON i.id = m.item_id WHERE e.name = 'Financial services'""")}
    assert "AI Act deadline" not in fs_items                           # not named in that item, dropped there
    assert st.dropped_generic >= 1 and st.dropped_not_named >= 1
    rels = db.execute("""SELECT s.name, r.relation, t.name FROM relations r JOIN entities s ON s.id = r.src_id
                         JOIN entities t ON t.id = r.dst_id""").fetchall()
    assert ("Model Context Protocol", "RELEASED_BY", "Anthropic") in rels  # reversed by the model, flipped
    assert ("Model Context Protocol", "ADOPTED_IN", "Financial services") in rels
    assert st.flipped_relations == 6


def test_movers_scores_confidence_and_evidence(store):
    corpus, db, _ = store
    sg.rebuild(corpus, db)
    up = {m.entity: m for m in sg.top_movers(db, 7, as_of=AS_OF)}
    mcp = up["Model Context Protocol"]
    assert (mcp.current, mcp.baseline, mcp.families) == (6, 1.0, 2)
    assert mcp.score == pytest.approx((6 - 1) / 2 ** 0.5, abs=0.01)
    assert mcp.confidence == "medium" and len(mcp.evidence) == 3
    assert "Kubernetes" not in up
    down = {m.entity for m in sg.top_movers(db, 7, as_of=AS_OF, direction="down")}
    assert "Kubernetes" in down
    reg = sg.top_movers(db, 7, entity_type="Regulation", min_current=1, as_of=AS_OF)
    assert reg[0].entity == "EU AI Act" and reg[0].new and reg[0].confidence == "low"


def test_trend_clusters_and_reading_list(store):
    corpus, db, _ = store
    sg.rebuild(corpus, db)
    t = sg.trend_metrics("mcp", db, 7, periods=5, as_of=AS_OF)
    assert t["entity"] == "Model Context Protocol"
    assert [p["mentions"] for p in t["series"]] == [1, 1, 1, 1, 6]  # one per baseline week, then the jump
    assert t["velocity"] == 5.0
    assert any(r["entity"] == "Stripe" for r in t["related"])
    pairs = [c["pair"] for c in sg.cooccurrence_growth(db, 7, as_of=AS_OF)]
    assert ["Anthropic", "Stripe"] in [sorted(p) for p in pairs]
    top = sg.reading_list(db, days=7, limit=3, as_of=AS_OF)
    assert top[0]["title"] == "AI Act deadline"  # relevance 3 for the lens wins
    assert sg.trend_metrics("does-not-exist", db) is None


def test_graph_payload_links_items_entities_and_relations(store):
    from signal_radar.graph_export import batch_payload

    corpus, db, _ = store
    sg.rebuild(corpus, db)
    p = batch_payload(db, min_mentions=2)
    node_ids = {n["id"] for n in p["nodes"]}
    assert "ent:modelcontextprotocol" in node_ids and "ent:euaiact" not in node_ids  # 1 mention < min_mentions
    mentions = [e for e in p["edges"] if e["relation"] == "MENTIONS"]
    assert all(e["target"] in node_ids for e in mentions)
    assert all(len(e["source"]) == 16 for e in mentions)  # item ids = Graqle document node ids
    assert {"source": "ent:modelcontextprotocol", "target": "ent:anthropic", "relation": "RELEASED_BY"} in p["edges"]



def test_reading_list_suppresses_same_story_from_two_editions(tmp_path):
    corpus, db = Corpus(tmp_path / "c"), connect(tmp_path / "s.sqlite")
    a = mk("Meta Launches an Enterprise AI Platform", "Meta enterprise platform.", AS_OF, "tldr_ai", "https://x.com/a")
    b = mk("Meta launches enterprise AI platform, hires MongoDB CEO", "Meta enterprise platform.", AS_OF, "tldr_tech",
           "https://y.com/b")
    c = mk("Unrelated story about banks", "Banks.", AS_OF, "tldr_ai", "https://z.com/c")
    corpus.write([a, b, c])
    canned = {a.title: ex(("Meta", "Company")), b.title: ex(("Meta", "Company")), c.title: ex(category="financial_services")}
    extract_items(corpus=corpus, db=db, complete=FakeModel(canned), taxonomy=Taxonomy.load())
    sg.rebuild(corpus, db)
    titles = [r["title"] for r in sg.reading_list(db, days=7, as_of=AS_OF)]
    assert len(titles) == 2 and "Unrelated story about banks" in titles



def test_brand_prefix_merge_only_for_versioned_names():
    m = sg.merge_brand_prefixes({"opus5.5", "claudeopus5.5", "code", "claudecode", "sol", "gpt6sol", "6sol", "qwen3"})
    assert m == {"opus5.5": "claudeopus5.5", "6sol": "gpt6sol"}  # "code"/"sol" are unversioned; "qwen3" has no twin
    assert sg.merge_brand_prefixes({"mcpservers", "mcpserver", "news", "new"}) == {"mcpservers": "mcpserver"}


def test_min_relevance_filters_movers(store):
    corpus, db, _ = store
    sg.rebuild(corpus, db)
    assert any(m.entity == "Model Context Protocol" for m in sg.top_movers(db, 7, as_of=AS_OF, min_relevance=2))
    assert [m.entity for m in sg.top_movers(db, 7, as_of=AS_OF, min_relevance=3, min_current=1)] == ["EU AI Act"]


def test_votes_rerank_after_three_and_explain(store):
    corpus, db, _ = store
    sg.rebuild(corpus, db)
    base = {r["title"]: r for r in sg.reading_list(db, days=7, limit=20, as_of=AS_OF)}
    k8s = [r[0] for r in db.execute("SELECT id FROM items WHERE title LIKE 'MCP launch%' ORDER BY id")]
    db.execute("INSERT INTO feedback VALUES (?, 1, 'now')", (k8s[0],))
    db.execute("INSERT INTO feedback VALUES (?, 1, 'now')", (k8s[1],))
    db.commit()
    two = {r["title"]: r for r in sg.reading_list(db, days=7, limit=20, as_of=AS_OF)}
    assert all(r["personal"] == 0 for r in two.values())  # two votes: stored, not used yet
    db.execute("INSERT INTO feedback VALUES (?, -1, 'now')", (db.execute("SELECT id FROM items WHERE title = 'AI Act deadline'").fetchone()[0],))
    db.commit()
    three = {r["title"]: r for r in sg.reading_list(db, days=7, limit=20, as_of=AS_OF)}
    title = next(t for t in three if t.startswith("MCP launch"))  # near-identical titles: one survives deduping
    mcp = three[title]
    assert mcp["personal"] > 0 and "you liked items about" in mcp["because"] and mcp["score"] > base[title]["score"]
    act = three["AI Act deadline"]
    assert act["personal"] < 0 and "dismissed" in act["because"]
    assert abs(act["personal"]) <= 0.8  # bounded: relevance to the lens still dominates
