"""Build a self-contained demo instance from synthetic news, with no mailbox, API key or local model.

    python scripts/demo.py            # writes demo/ (corpus, signal store, brief, console and public sites)
    python scripts/demo.py --serve    # and opens the console on http://127.0.0.1:8765

Every item is invented: fictional companies (Contoso, Northwind, Fabrikam...), example.com links. The storylines
are shaped so each signal type shows up: rising (MCP in banking), newly emerging (agent registries), fading
(prompt engineering), a forming cluster (MCP with model risk), steady background noise. Extraction is canned
(what the `extraction` role would return), so the rest of the pipeline runs exactly as in a real instance.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
os.environ["SIGNAL_RADAR_HOME"] = str(DEMO)  # before importing signal_radar: every path resolves under demo/
sys.path.insert(0, str(ROOT / "src"))

from signal_radar import signals as sg  # noqa: E402
from signal_radar.brief import build_brief  # noqa: E402
from signal_radar.corpus import Corpus  # noqa: E402
from signal_radar.extract import Taxonomy, connect, extract_items  # noqa: E402
from signal_radar.items import Item, item_id  # noqa: E402
from signal_radar.site.build import build_site  # noqa: E402

WEEKS = 8
SOURCES = {"wire": "AI Wire (demo)", "digest": "Daily Tech Digest (demo)", "policywatch": "Policy Watch (demo)",
           "eurotech": "EuroTech Brief (demo)"}

# One storyline = entities, category, relevance, items per week (oldest -> newest), title and summary templates.
# Titles walk through every template x organisation ({o}) pair, so they stay distinct; {i} numbers routine items.
STORIES = [
    dict(key="mcp_banks", why="Agent adoption in banking: how regulated firms connect AI to core systems.", weekly=[1, 1, 1, 2, 2, 3, 5, 8], category="agents_devtools", relevance=2,
         sources=["wire", "digest", "eurotech"],
         entities=[("Model Context Protocol", "Technology"), ("Banking", "Industry")],
         orgs=["Northwind Bank", "Woodgrove Bank", "Contoso AI", "Fabrikam Cloud"],
         rels=[("Model Context Protocol", "ADOPTED_IN", "Banking")],
         titles=["{o} connects its agents to core banking through MCP servers",
                 "{o} opens an MCP gateway for payments and treasury data",
                 "Why {o} standardised on the Model Context Protocol",
                 "{o} pilots MCP-based agents in its back office",
                 "{o} publishes reference MCP servers for regulated data",
                 "{o} logs every MCP tool call for audit"],
         summary="An MCP rollout in banking: agents get governed access to internal systems through one protocol."),
    dict(key="mcp_risk", why="Model risk management extends to agent tools, a direct governance question.", weekly=[0, 0, 0, 0, 1, 1, 2, 4], category="security_risk", relevance=3,
         sources=["policywatch", "wire"],
         entities=[("Model Context Protocol", "Technology"), ("Model risk", "Concept"), ("Financial services", "Industry")],
         orgs=["Woodgrove Bank", "Northwind Bank", "Tailspin Audit"],
         rels=[],
         titles=["{o} adds MCP tool calls to its model risk inventory",
                 "Model risk teams at {o} ask who approves an agent's tools",
                 "{o} maps MCP servers to its model risk framework"],
         summary="Model risk management meets agent tooling: tool access becomes part of model validation."),
    dict(key="ai_act", why="EU AI Act obligations for financial services.", weekly=[0, 1, 1, 1, 2, 3, 4, 5], category="regulation_governance", relevance=3,
         sources=["policywatch", "eurotech"],
         entities=[("EU AI Act", "Regulation"), ("AI Office", "Company"), ("Financial services", "Industry")],
         orgs=["the AI Office", "Woodgrove Insurance", "Northwind Bank", "a supervisors' working group"],
         rels=[("EU AI Act", "REGULATES", "Financial services")],
         titles=["EU AI Act: {o} sets out what high-risk credit scoring must document",
                 "EU AI Act readiness: {o} publishes its conformity checklist",
                 "EU AI Act guidance from {o} on general-purpose model obligations",
                 "EU AI Act and AI governance: {o} names an accountable executive",
                 "EU AI Act timelines: {o} plans its model inventory"],
         summary="EU AI Act obligations move from text to practice; firms document AI governance and model risk."),
    dict(key="agent_registry", why="A new AI governance control: inventories of autonomous agents.", weekly=[0, 0, 0, 0, 0, 1, 3, 4], category="regulation_governance", relevance=3,
         sources=["policywatch", "wire", "digest"],
         entities=[("Agent registry", "Concept"), ("AI governance", "Concept"), ("ISO 42001", "Regulation")],
         orgs=["Contoso AI", "Woodgrove Insurance", "Fabrikam Cloud", "Tailspin Audit"],
         rels=[("ISO 42001", "REGULATES", "Agent registry")],
         titles=["{o} launches an agent registry for AI governance under ISO 42001",
                 "Agent registry: {o} inventories every autonomous agent it runs",
                 "{o}: an agent registry is the new model inventory"],
         summary="Agent registries appear as the control point for AI governance: one inventory of every agent."),
    dict(key="dora", why="DORA third-party risk now covers AI vendors.", weekly=[1, 1, 1, 1, 2, 2, 2, 3], category="financial_services", relevance=3,
         sources=["policywatch", "eurotech"],
         entities=[("DORA", "Regulation"), ("Insurance", "Industry")],
         orgs=["Woodgrove Insurance", "Northwind Bank", "Fabrikam Cloud"],
         rels=[("DORA", "REGULATES", "Insurance")],
         titles=["DORA third-party risk: {o} renegotiates its AI vendor contracts",
                 "DORA register of information: {o} adds AI model providers",
                 "{o} tests exit plans for AI suppliers under DORA",
                 "DORA incident reporting: {o} adds AI outages",
                 "{o} maps critical AI services for DORA"],
         summary="DORA puts AI vendors in scope of ICT third-party risk: registers, exit plans, contract clauses."),
    dict(key="langgraph", why="A hands-on skill for building agent systems.", weekly=[1, 1, 2, 2, 2, 3, 3, 4], category="agents_devtools", relevance=1,
         sources=["digest", "wire"],
         entities=[("LangGraph", "Technology"), ("Multi-agent systems", "Concept"), ("Agent orchestration", "Skill")],
         orgs=["Tailspin Labs", "Contoso AI", "a community tutorial"],
         rels=[("LangGraph", "ENABLES", "Multi-agent systems"), ("Multi-agent systems", "REQUIRES_SKILL", "Agent orchestration")],
         titles=["{o} shows a multi-agent workflow in LangGraph with human approval steps",
                 "Building supervisor agents in LangGraph: notes from {o}",
                 "{o} benchmarks multi-agent orchestration patterns",
                 "Agent orchestration in practice: {o} on retries and handoffs",
                 "{o} open-sources a LangGraph template for document review",
                 "Testing multi-agent systems: lessons from {o}"],
         summary="Hands-on multi-agent orchestration: supervisors, handoffs and checkpoints."),
    dict(key="k8s", why="Infrastructure detail with little enterprise angle.", weekly=[4, 4, 3, 3, 2, 2, 1, 0], category="ai_infrastructure", relevance=1,
         sources=["digest"],
         entities=[("Kubernetes", "Technology"), ("GPU scheduling", "Concept")],
         orgs=["Fabrikam Cloud", "Litware Systems", "Tailspin Labs", "Adventure Works", "Proseware"],
         rels=[],
         titles=["{o} tunes Kubernetes GPU scheduling for inference",
                 "{o} shares its Kubernetes cost dashboard for GPUs",
                 "{o} moves training jobs to a new Kubernetes operator",
                 "Kubernetes autoscaling for model serving at {o}"],
         summary="Kubernetes operations for AI workloads."),
    dict(key="prompting", why="General technique, no enterprise angle.", weekly=[4, 4, 3, 3, 2, 2, 1, 0], category="agents_devtools", relevance=1,
         sources=["digest", "wire"],
         entities=[("Prompt engineering", "Skill")],
         orgs=["Litware Systems", "Adventure Works", "Tailspin Labs", "Proseware", "Lucerne Publishing"],
         rels=[],
         titles=["Ten prompt engineering tricks from {o}",
                 "{o} retires its prompt engineering playbook",
                 "{o} on prompt engineering for long documents",
                 "Prompt engineering patterns {o} still uses"],
         summary="Prompt engineering techniques."),
    dict(key="models", why="Routine model news.", weekly=[3, 3, 3, 3, 3, 3, 3, 3], category="ai_models_labs", relevance=1,
         sources=["wire", "digest"],
         entities=[("Contoso AI", "Company"), ("Contoso Nova", "Product")],
         orgs=["Contoso AI"],
         rels=[("Contoso Nova", "RELEASED_BY", "Contoso AI")],
         titles=["{o} ships Contoso Nova update {i}",
                 "Contoso Nova benchmark round {i} from {o}",
                 "{o} cuts Contoso Nova prices, round {i}"],
         summary="A routine model release and pricing change."),
    dict(key="vectors", why="Data platform news relevant to enterprise architecture.", weekly=[2, 2, 2, 2, 2, 2, 2, 2], category="data_platforms", relevance=2,
         sources=["digest"],
         entities=[("Vector databases", "Technology"), ("Tailspin DB", "Product")],
         orgs=["Tailspin DB", "Fabrikam Cloud"],
         rels=[],
         titles=["{o} adds hybrid search to its vector database, release {i}",
                 "Vector databases compared by {o}, part {i}"],
         summary="Vector search and data platform news."),
    dict(key="consumer", why="Consumer news.", weekly=[3, 3, 3, 3, 3, 3, 3, 3], category="consumer_media", relevance=0,
         sources=["digest"],
         entities=[("Litware", "Company"), ("Litware Phone", "Product")],
         orgs=["Litware"],
         rels=[],
         titles=["{o} teases a new AI camera mode, take {i}",
                 "{o} adds an AI photo editor, version {i}"],
         summary="Consumer device news."),
    dict(key="funding", why="Market background.", weekly=[2, 2, 2, 2, 2, 2, 2, 2], category="startups_funding", relevance=1,
         sources=["eurotech", "digest"],
         entities=[("Venture funding", "Concept")],
         orgs=["an Amsterdam agent startup", "a Berlin compliance startup", "a Paris inference startup"],
         rels=[],
         titles=["{o} raises a seed round, deal {i}", "{o} closes its Series A, deal {i}"],
         summary="A startup funding round."),
]


def build_items(as_of: date) -> tuple[list[Item], dict[str, dict]]:
    rnd = random.Random(42)  # same demo on every run
    items, canned, n = [], {}, 0
    for story in STORIES:
        i, nt = 0, len(story["titles"])
        assert sum(story["weekly"]) <= nt * len(story["orgs"]) or "{i}" in story["titles"][0], story["key"]
        for w, count in enumerate(story["weekly"]):
            week_end = as_of - timedelta(days=7 * (WEEKS - 1 - w))
            for k in range(count):
                n, i = n + 1, i + 1
                org = story["orgs"][(i // nt) % len(story["orgs"])]
                title = story["titles"][i % nt].format(o=org, i=i)
                title = title[0].upper() + title[1:]
                day = week_end - timedelta(days=rnd.randrange(7))
                source = story["sources"][(w + k) % len(story["sources"])]
                url = f"https://example.com/demo/{story['key']}/{n}"
                items.append(Item(id=item_id(url, title), source_id=source, date=day, url=url, title=title,
                                  summary=f"{story['summary']} (Synthetic demo item.)", edition=SOURCES[source],
                                  tags=["demo"]))
                orgs = [(org, "Company")] if org[0].isupper() and org not in {e for e, _ in story["entities"]} else []
                canned[title] = {
                    "category": story["category"], "secondary_category": None, "relevance": story["relevance"],
                    "relevance_reason": story["why"], "entities": [{"name": e, "type": t} for e, t in story["entities"] + orgs],
                    "relations": [{"source": s, "relation": r, "target": t} for s, r, t in story["rels"]]}
    return items, canned


def canned_model(canned: dict[str, dict]):
    """Stands in for the `extraction` role: the prompt's first line is the item title."""
    def complete(role, prompt, system=None, json_schema=None):
        title = prompt.splitlines()[0].lstrip("# ").strip()
        return SimpleNamespace(data=canned[title], provider="demo", model="canned", latency_ms=0)
    return complete


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--serve", action="store_true", help="serve the console after building")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if DEMO.exists():
        shutil.rmtree(DEMO)
    shutil.copytree(ROOT / "config", DEMO / "config")
    publish = DEMO / "config" / "publish.yaml"  # the public edition's footer says what this is
    text = publish.read_text().replace("publisher: Your Name", "publisher: Signal Radar demo")
    publish.write_text(re.sub(r"notice: >-\n(  .*\n)+", "notice: >-\n  {publisher} · Synthetic data: every company, headline "
                              "and link is invented to show how the pipeline works.\n", text))
    as_of = date.today()
    items, canned = build_items(as_of)
    corpus, db = Corpus(DEMO / "corpus"), connect(DEMO / "data" / "signals.sqlite")
    corpus.write(items)
    stats = extract_items(corpus=corpus, db=db, complete=canned_model(canned), taxonomy=Taxonomy.load())
    sg.rebuild(corpus, db)
    build_brief(db, prose=False)  # signals and evidence only; prose needs the `writing` role
    console = build_site(narrate=False, db=db)
    public = build_site(edition="public", narrate=False)
    print(f"demo: {len(items)} synthetic items, {stats.done} extractions\n"
          f"  console site: {console.get('out', DEMO / 'data' / 'site')}\n  public site:  {public.get('out')}")
    if args.serve:
        import uvicorn

        from signal_radar.site.web import create_app

        print(f"Signal Radar demo console: http://127.0.0.1:{args.port}/  (Ctrl+C to stop)", flush=True)
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
    else:
        print("  serve it:     python scripts/demo.py --serve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
