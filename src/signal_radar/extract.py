"""Entity extraction: one corpus item -> entities, relations, category and relevance, via the `extraction` role.

The model's raw JSON is stored per item (table `extractions` in data/signals.sqlite) together with the model name
and an input hash. The signal store is rebuilt from those rows deterministically (signals.rebuild), so changing
alias rules or the canonicalisation never needs another LLM call; changing the taxonomy or the prompt does
(`--redo`), and the input hash tells which items are stale.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .config import CONFIG_DIR, DATA_DIR
from .corpus import Corpus

SIGNALS_DB = DATA_DIR / "signals.sqlite"
PROMPT_VERSION = "extract-v3"

# Direction and allowed types per relation; the prompt states them and signals.rebuild enforces them.
RELATION_RULES: dict[str, tuple[set[str], set[str], str]] = {
    "RELEASED_BY": ({"Product", "Technology"}, {"Company", "Person"}, "product -> the company that made it"),
    "COMPETES_WITH": ({"Company", "Product"}, {"Company", "Product"}, "company/product -> a rival"),
    "ENABLES": ({"Technology", "Product", "Concept"}, {"Concept", "Product", "Technology", "Industry"}, "technology -> what it makes possible"),
    "BUILT_ON": ({"Product", "Technology"}, {"Technology", "Product"}, "product -> the technology/product underneath"),
    "REGULATES": ({"Regulation", "Company"}, {"Industry", "Technology", "Company", "Product", "Concept"}, "law or regulator -> what it governs"),
    "ADOPTED_IN": ({"Technology", "Product", "Concept"}, {"Industry"}, "technology -> an industry adopting it"),
    "REQUIRES_SKILL": ({"Technology", "Concept", "Product"}, {"Skill"}, "technology -> a skill needed to use it"),
}


@dataclass
class Taxonomy:
    categories: dict[str, str]
    lens: str
    relevance_scale: dict[int, str]
    entity_types: list[str]
    relations: list[str]
    watchlist: list[str] = field(default_factory=list)
    lens_categories: list[str] = field(default_factory=list)
    lens_targets: list[str] = field(default_factory=list)   # graph signals only (ADR-0018); not in the fingerprint

    @classmethod
    def load(cls, path: Path | None = None) -> Taxonomy:
        d = yaml.safe_load((path or CONFIG_DIR / "taxonomy.yaml").read_text())
        return cls(d["categories"], d["lens"].strip(), {int(k): v for k, v in d["relevance_scale"].items()},
                   d["entity_types"], d["relations"], d.get("watchlist", []), d.get("lens_categories", []),
                   d.get("lens_targets", []))

    def fingerprint(self) -> str:
        blob = json.dumps([self.categories, self.lens, self.relevance_scale, self.entity_types, self.relations, PROMPT_VERSION])
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


def schema(tax: Taxonomy) -> dict:
    cats = list(tax.categories)
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": cats},
            "secondary_category": {"type": ["string", "null"], "enum": cats + [None]},
            "relevance": {"type": "integer", "enum": sorted(tax.relevance_scale)},
            "relevance_reason": {"type": "string"},
            "entities": {"type": "array", "maxItems": 8, "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "type": {"type": "string", "enum": tax.entity_types}},
                "required": ["name", "type"], "additionalProperties": False}},
            "relations": {"type": "array", "maxItems": 3, "items": {
                "type": "object",
                "properties": {"source": {"type": "string"}, "relation": {"type": "string", "enum": tax.relations},
                               "target": {"type": "string"}},
                "required": ["source", "relation", "target"], "additionalProperties": False}},
        },
        "required": ["category", "secondary_category", "relevance", "relevance_reason", "entities", "relations"],
        "additionalProperties": False,
    }


def system_prompt(tax: Taxonomy) -> str:
    cats = "\n".join(f"- {k}: {v}" for k, v in tax.categories.items())
    scale = "\n".join(f"- {k}: {v}" for k, v in sorted(tax.relevance_scale.items()))
    rels = "\n".join(f"- A {r} B: {rule[2]}" for r, rule in RELATION_RULES.items() if r in tax.relations)
    return f"""You classify one tech news item and extract its named entities for a market-intelligence graph.

1. category: the single best fit for what the item is mainly about:
{cats}
   secondary_category: a second clear fit, or null.
2. relevance (integer) for this reader: {tax.lens}
   Judge what the item is about, not what the reader likes. Most items score 0 or 1. Use the scale strictly:
{scale}
   relevance_reason: one short sentence, max 15 words, about the item. Mention banking, insurance, regulation
   or governance only if the item itself does.
3. entities: the specific named things the item is about, at most 8, most important first.
   Types: Company (organisations, labs, vendors, banks, regulators), Product (named products, models, tools,
   open-source projects), Technology (technical categories, e.g. "vector database"), Concept (ideas, patterns,
   e.g. "agentic workflows"), Regulation (laws, rules, standards, e.g. "EU AI Act"), Industry (sectors, e.g.
   "banking"), Skill (learnable human skills), Person (named people).
   Every entity must be named in the item text itself. Never add the reader's industry or interests as
   entities; they are only for the relevance score. Use the common full name ("Google DeepMind",
   "Claude Opus 5.5"). Only specific names: no generic words ("AI", "LLM", "model", "startup"), no groups or
   roles ("investors", "founders", "users", "subscribers", "developers"), no activities ("coding tasks").
4. relations: only ones the item states, between your entities, at most 3. Direction matters:
{rels}
Use only what the item says."""


def item_prompt(meta: dict, body: str) -> str:
    text = "\n".join(ln for ln in body.strip().splitlines() if not ln.startswith("[Source]("))
    return text.strip()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path if path is not None else SIGNALS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS extractions (
        item_id TEXT PRIMARY KEY, model TEXT NOT NULL, taxonomy TEXT NOT NULL, input_hash TEXT NOT NULL,
        data TEXT NOT NULL, latency_ms INTEGER, created_at TEXT NOT NULL)""")
    # The user's votes survive rebuilds (rebuild only replaces its own tables).
    con.execute("CREATE TABLE IF NOT EXISTS feedback (item_id TEXT PRIMARY KEY, vote INTEGER NOT NULL, at TEXT NOT NULL)")
    # Generated prose (theme narratives, answers) cached by input hash, so a rebuild doesn't re-call a model.
    con.execute("""CREATE TABLE IF NOT EXISTS narrations (input_hash TEXT PRIMARY KEY, kind TEXT, model TEXT,
        data TEXT NOT NULL, created_at TEXT NOT NULL)""")
    return con


def _input_hash(prompt: str, tax: Taxonomy) -> str:
    return hashlib.sha256(f"{tax.fingerprint()}\n{prompt}".encode()).hexdigest()[:16]


@dataclass
class ExtractStats:
    candidates: int = 0
    done: int = 0
    failed: int = 0
    skipped_current: int = 0


def extract_items(
    item_ids: Iterable[str] | None = None,
    *,
    corpus: Corpus | None = None,
    complete: Callable[..., object] | None = None,
    db: sqlite3.Connection | None = None,
    taxonomy: Taxonomy | None = None,
    redo: bool = False,
    limit: int | None = None,
    workers: int = 1,
    model_overrides: list[str] | None = None,
    log: Callable[[str], None] = lambda m: None,
) -> ExtractStats:
    """Extract every corpus item that has no current extraction (or the given ids). Resumable at any point."""
    corpus = corpus if corpus is not None else Corpus()
    tax = taxonomy or Taxonomy.load()
    db = db if db is not None else connect()
    if complete is None:
        from .llm import LLMRouter

        router = LLMRouter.from_config(overrides=model_overrides, job="extract")
        complete = router.complete
    sys_prompt, js = system_prompt(tax), schema(tax)
    existing = {row[0]: row[1] for row in db.execute("SELECT item_id, input_hash FROM extractions")}
    wanted = set(item_ids) if item_ids is not None else None

    todo: list[tuple[str, str, str]] = []
    stats = ExtractStats()
    for meta, body in corpus.iter_meta():
        if wanted is not None and meta["id"] not in wanted:
            continue
        stats.candidates += 1
        prompt = item_prompt(meta, body)
        h = _input_hash(prompt, tax)
        if not redo and existing.get(meta["id"]) == h:
            stats.skipped_current += 1
            continue
        todo.append((meta["id"], prompt, h))
    # Newest first: the most recent weeks matter most if a long run is interrupted.
    todo.reverse()
    if limit is not None:
        todo = todo[:limit]

    lock = threading.Lock()

    def one(item_id: str, prompt: str, h: str):
        res = complete("extraction", prompt, system=sys_prompt, json_schema=js)
        return item_id, h, res

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, *t) for t in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                item_id, h, res = fut.result()
                data = getattr(res, "data", None)
                if not isinstance(data, dict):
                    raise ValueError("no JSON object in response")
                with lock:
                    db.execute(
                        "INSERT OR REPLACE INTO extractions VALUES (?,?,?,?,?,?,?)",
                        (item_id, f"{res.provider}/{res.model}", tax.fingerprint(), h, json.dumps(data),
                         getattr(res, "latency_ms", None), datetime.now(UTC).isoformat()),
                    )
                    db.commit()
                stats.done += 1
            except Exception as e:  # noqa: BLE001 - one bad item must not stop a long run
                stats.failed += 1
                log(f"failed: {type(e).__name__}: {str(e)[:200]}")
            if n % 25 == 0:
                log(f"{n}/{len(todo)} done ({stats.failed} failed)")
    return stats
