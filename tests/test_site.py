import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from signal_radar import signals as sg
from signal_radar.extract import Taxonomy
from signal_radar.site.build import build_site
from signal_radar.site.data import build_snapshot, entity_detail, entity_slug, watch_count
from signal_radar.site.prose import _clean, answer, narrate_theme, retrieve
from signal_radar.site.themes import detect_themes
from signal_radar.site.web import create_app
from test_signals import AS_OF, store  # noqa: F401 - pytest fixture


def fake_writer(data):
    calls = []

    def complete(role, prompt, system=None, json_schema=None):
        calls.append(role)
        if role == "writing":
            from signal_radar.llm import LLMError
            raise LLMError("no key")  # the cloud role is unavailable; the local one answers
        return SimpleNamespace(data=data(prompt) if callable(data) else data, provider="ollama", model="fake")
    complete.calls = calls
    return complete


@pytest.fixture
def built(store, tmp_path):  # noqa: F811
    corpus, db, _ = store
    sg.rebuild(corpus, db)
    return corpus, db


def test_themes_lens_first_then_momentum(built):
    _, db = built
    themes = detect_themes(db, 7, n=3, as_of=AS_OF, min_items=1)
    lens = themes[0]
    rel = dict(db.execute("SELECT id, relevance FROM items").fetchall())
    assert lens.kind == "lens" and all(rel[i] >= 2 for i in lens.item_ids)  # built only from relevant items
    assert lens.seed == "Anthropic"  # most relevance in the window (6 MCP launch items rated 2), ties by name
    assert [t.kind for t in themes[1:]] == ["momentum"] * (len(themes) - 1)
    assert all(len(set(a.item_ids) & set(b.item_ids)) <= 0.5 * len(b.item_ids) for a in themes for b in themes if a is not b)


def test_narration_falls_back_to_local_and_is_cached(built):
    _, db = built
    t = detect_themes(db, 7, n=1, as_of=AS_OF, min_items=1, lens_first=False)[0]
    w = fake_writer(lambda p: {"title": "MCP spreads to banks — fast", "narrative": "Two sentences.",
                               "so_what": "Matters.", "cited_ids": [t.items[0]["id"], "not-an-id"]})
    out = narrate_theme(db, t, "lens", complete=w)
    assert out["title"] == "MCP spreads to banks, fast"         # house style: no em dash
    assert out["cited_ids"] == [t.items[0]["id"]]                # unknown ids dropped
    assert out["role"] == "extraction" and w.calls == ["writing", "extraction"]
    again = narrate_theme(db, t, "lens", complete=w)
    assert again["title"] == out["title"] and len(w.calls) == 2  # cached, no new call


def test_ask_is_grounded_and_shows_its_trace(built):
    _, db = built
    ev = retrieve(db, "What is happening with MCP and Stripe?")
    assert "Model Context Protocol" in ev["entities"] or "Stripe" in ev["entities"]
    known = ev["items"][0]["id"]
    w = fake_writer({"answer": f"MCP is spreading [{known}] [ffffffffffffffff]. }}", "cited_ids": [known, "ffffffffffffffff"],
                     "evidence_is_thin": False})
    a = answer(db, "What is happening with MCP and Stripe?", "lens", complete=w)
    assert a["answer"] == f"MCP is spreading [{known}] ."  # invented id removed, stray brace removed
    assert [c["id"] for c in a["cited"]] == [known]
    assert any(t.startswith("read ") for t in a["trace"])
    none = answer(db, "Quantum basket weaving outlook?", "lens", complete=w)
    assert none["evidence_is_thin"] and none["cited"] == []


def test_snapshot_and_entity_detail(built):
    _, db = built
    snap = build_snapshot(db, Taxonomy.load(), narrate=False)
    assert snap["meta"]["as_of"] == AS_OF.isoformat()
    assert {"themes", "reading", "movers", "blind_spots", "market"} <= set(snap)
    assert any(w["term"] == "EU AI Act" and w["mentions_90d"] == 1 for w in snap["blind_spots"]["watchlist"])
    assert len(snap["movers"]["up"][0]["series"]) == 12
    d = entity_detail(db, "Model Context Protocol")
    assert d["slug"] == "modelcontextprotocol" and any(n["name"] == "Anthropic" for n in d["neighbours"])


def test_watch_count_matches_whole_words_and_case_for_acronyms():
    texts = ["The SEC fined a bank", "a second look", "EU AI Act guidance", "explained simply"]
    assert watch_count("SEC", texts) == 1
    assert watch_count("EU AI Act", texts) == 1
    assert watch_count("explainability", texts) == 0
    assert entity_slug("c#") == "c" and entity_slug("claudeopus5.5") == "claudeopus5.5"


def test_public_edition_drops_newsletter_text_and_live_features(built, tmp_path):
    _, db = built
    pub = tmp_path / "pub"
    build_site(pub, edition="public", narrate=False, db=db)
    snap = json.loads((pub / "data" / "snapshot.json").read_text())
    assert all("summary" not in i for t in snap["themes"] for i in t["items"])
    assert all("summary" not in r for r in snap["reading"])
    html = (pub / "index.html").read_text()
    assert "console.js" not in html and 'id="ask"' not in html and "data-vote" not in html
    con = tmp_path / "con"
    build_site(con, edition="console", narrate=False, db=db)
    assert "console.js" in (con / "index.html").read_text()
    assert (con / "data" / "entity" / "modelcontextprotocol.json").exists()


def test_console_api_votes_and_ask(built, tmp_path):
    _, db = built
    site = tmp_path / "site"
    build_site(site, narrate=False, db=db)
    db_path = tmp_path / "signals.sqlite"  # the store fixture's database file
    item = db.execute("SELECT id FROM items LIMIT 1").fetchone()[0]
    w = fake_writer({"answer": "ok", "cited_ids": [], "evidence_is_thin": True})
    client = TestClient(create_app(site, db_path, complete=w))
    assert client.get("/").status_code == 200 and "SIGNAL RADAR" in client.get("/").text
    assert client.post("/api/feedback", json={"item_id": item, "vote": -1}).json() == {"ok": True}
    assert client.get("/api/votes").json() == {item: -1}
    assert client.post("/api/feedback", json={"item_id": item, "vote": 0}).json() == {"ok": True}
    assert client.get("/api/votes").json() == {}
    assert client.post("/api/feedback", json={"item_id": "0" * 16, "vote": 1}).status_code == 404
    assert client.post("/api/feedback", json={"item_id": "../etc", "vote": 1}).status_code == 422
    assert client.post("/api/ask", json={"question": "What about MCP?"}).json()["answer"] == "ok"


def test_clean_keeps_citations():
    assert _clean("done [96bb22dbcba2879c] }") == "done [96bb22dbcba2879c]"


def test_single_file_is_self_contained_public_and_hash_routed(built, tmp_path):
    from signal_radar.site.build import build_single_file

    _, db = built
    out = tmp_path / "share.html"
    res = build_single_file(out, db=db, narrate=False)
    html = out.read_text()
    assert res["entities"] >= 1 and html.startswith("<!doctype html>")
    assert 'href="static/' not in html and "fetch(`data/entity" in html  # css inline; fetch only as fallback
    assert 'id="sr-data"' in html and '"modelcontextprotocol"' in html  # neighbourhoods embedded
    assert 'href="#explore:' in html and 'href="explore.html' not in html  # in-file navigation
    assert "MCP servers for banks" not in html  # newsletter summary text stays out
    assert 'id="ask"' not in html and "data-vote" not in html
    data = html.split('<script id="sr-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert "</" not in data  # embedded JSON cannot close its script tag
