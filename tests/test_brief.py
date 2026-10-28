"""Weekly brief: computed sections, evidence-checked prose, outputs, MCP prompts (CLAUDE.md analysis jobs)."""

import json
from types import SimpleNamespace

import anyio
from mcp import Client

from signal_radar import brief as B
from signal_radar import signals as sg
from test_signals import AS_OF, store  # noqa: F401 - pytest fixture


def built(store, tmp_path, monkeypatch):  # noqa: F811
    corpus, db, _ = store
    sg.rebuild(corpus, db)
    monkeypatch.setattr(B, "BRIEFS_DIR", tmp_path / "briefs")
    monkeypatch.setattr(B, "BRIEF_DATA_DIR", tmp_path / "briefdata")
    return db


def writer(data):
    def complete(role, prompt, system=None, json_schema=None):
        return SimpleNamespace(data=data(prompt), provider="fake", model="w", candidate="w")
    return complete


def test_collect_is_computed_and_carries_evidence(store, tmp_path, monkeypatch):  # noqa: F811
    db = built(store, tmp_path, monkeypatch)
    d = B.collect(db, AS_OF, 7)
    kinds = {s["kind"] for s in d["signals"]}
    assert {"rising", "fading"} & kinds
    mcp = next(s for s in d["signals"] if s["name"] == "Model Context Protocol")
    assert mcp["current"] == 6 and mcp["evidence"] and all("url" in e for e in mcp["evidence"])
    assert d["week"] == "2026-W40" and d["coverage"]["items"] >= 7


def test_prose_is_checked_against_signals_and_evidence(store, tmp_path, monkeypatch):  # noqa: F811
    db = built(store, tmp_path, monkeypatch)
    d = B.collect(db, AS_OF, 7)
    sid = next(s["sid"] for s in d["signals"] if s["name"] == "Model Context Protocol")
    real = next(s for s in d["signals"] if s["sid"] == sid)["evidence"][0]["id"]

    def fake(prompt):
        assert sid in prompt and real in prompt  # the model sees the computed signals and their evidence
        return {"headline": "MCP moved — fast.", "signals": [{"sid": sid, "so_what": "Tool makers adopt it."},
                                                             {"sid": "S999", "so_what": "invented"}],
                "learning": [{"sid": "S999", "why": "x", "project": "y"}],
                "opportunities": [{"idea": "MCP audit", "target_client": "banks", "problem": "p", "why_now": "w",
                                   "evidence_ids": [real, "ffffffffffffffff"]},
                                  {"idea": "Invented", "target_client": "x", "problem": "p", "why_now": "w",
                                   "evidence_ids": ["ffffffffffffffff"]}]}
    p = B.write_prose(d, "lens", complete=writer(fake))
    assert p["headline"] == "MCP moved, fast."                 # house style
    assert p["so_what"] == {sid: "Tool makers adopt it."}      # unknown signal dropped
    assert p["learning"] == []                                 # not a learning candidate
    assert [o["idea"] for o in p["opportunities"]] == ["MCP audit"] and p["opportunities"][0]["evidence_ids"] == [real]


def test_build_writes_markdown_and_json(store, tmp_path, monkeypatch):  # noqa: F811
    db = built(store, tmp_path, monkeypatch)
    out = B.build_brief(db, AS_OF, 7, prose=False)
    md = (tmp_path / "briefs" / "2026-W40.md").read_text()
    assert out["week"] == "2026-W40" and ("## Rising" in md or "## Watching" in md)
    assert "Evidence: [" in md and "Confidence" in md and "—" not in md
    assert json.loads((tmp_path / "briefdata" / "2026-W40.json").read_text())["signals"]
    assert B.latest_brief()["week"] == "2026-W40"


def test_public_brief_drops_newsletter_text(store, tmp_path, monkeypatch):  # noqa: F811
    from signal_radar.site import build as site_build

    db = built(store, tmp_path, monkeypatch)
    B.build_brief(db, AS_OF, 7, prose=False)
    assert site_build._brief_for("public") is None          # the console brief is never served publicly
    out = B.build_brief(db, AS_OF, 7, prose=False, edition="public")
    assert out["markdown"] is None and (tmp_path / "briefdata" / "2026-W40.public.json").exists()
    pub = site_build._brief_for("public")
    assert all("summary" not in e for s in pub["signals"] for e in s["evidence"])
    assert pub["by_sid"] and site_build._brief_for("console")["signals"][0]["evidence"][0].get("summary") is not None


def test_mcp_prompts_and_brief_resource(store, tmp_path, monkeypatch):  # noqa: F811
    from signal_radar.mcp_server import build_server

    db = built(store, tmp_path, monkeypatch)
    B.build_brief(db, AS_OF, 7, prose=False)

    async def go():
        async with Client(build_server()) as c:
            names = {p.name for p in (await c.list_prompts()).prompts}
            brief = await c.get_prompt("weekly_signal_brief", {"days": "7"})
            content = await c.get_prompt("content_ideas", {"topic": "MCP"})
            res = await c.read_resource("brief://latest")
            return names, brief.messages[0].content.text, content.messages[0].content.text, res.contents[0].text
    names, brief_text, content_text, md = anyio.run(go)
    assert names == {"weekly_signal_brief", "learning_radar", "content_ideas"}
    assert "top_movers" in brief_text and "Never present a signal without evidence" in brief_text
    assert "about MCP" in content_text and "em dashes" in content_text
    assert md.startswith("# Signal brief 2026-W40")


def test_weekly_due_is_this_weeks_monday_until_its_brief_exists(tmp_path):
    from datetime import date

    thursday = date(2026, 10, 15)
    assert B.weekly_due(thursday, tmp_path) == date(2026, 10, 12)
    assert B.weekly_due(date(2026, 10, 12), tmp_path) == date(2026, 10, 12)  # Monday itself
    (tmp_path / "2026-W42.md").write_text("# Signal brief 2026-W42")
    assert B.weekly_due(thursday, tmp_path) is None
    assert B.weekly_due(date(2026, 10, 19), tmp_path) == date(2026, 10, 19)  # next week is due again
