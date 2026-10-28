"""Generated prose: theme narratives and Ask answers. The only place the site calls a model.

Both go through the LLM router by role. Each tries its preferred role first (`writing` for narratives,
`reasoning` for answers) and falls back to `extraction` (local) when that role isn't usable, e.g. no API key.
The model that actually wrote a text is stored with it and shown in the UI. Results are cached in
signals.sqlite:narrations by input hash, so rebuilding the site re-uses them.

Grounding rules for both: only the supplied items may be used, every claim cites item ids, and thin evidence
is said out loud. House style from CLAUDE.md: no em/en dashes, no "delve", "leverage", filler adjectives.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from ..llm import LLMError
from ..vectors import rrf
from .themes import Theme

STYLE = ("Write plainly and concretely. No em dashes or en dashes, no 'delve', 'leverage', 'landscape', "
         "'game-changer', no filler adjectives, no symmetric sentence pairs.")

THEME_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "narrative": {"type": "string"},
        "so_what": {"type": "string"},
        "cited_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "narrative", "so_what", "cited_ids"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "cited_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_is_thin": {"type": "boolean"},
    },
    "required": ["answer", "cited_ids", "evidence_is_thin"],
    "additionalProperties": False,
}


def _clean(text: str) -> str:
    text = re.sub(r"\s*[—–]\s*", ", ", text)  # house style: no em/en dashes
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"[\s}\"']+$", "", text) if re.search(r"[}\"']\s*$", text) else text  # stray JSON in the prose


def _hash(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _cached(db: sqlite3.Connection, h: str) -> dict | None:
    row = db.execute("SELECT model, data FROM narrations WHERE input_hash = ?", (h,)).fetchone()
    return {"model": row[0], **json.loads(row[1])} if row else None


def _store(db: sqlite3.Connection, h: str, kind: str, model: str, data: dict) -> None:
    db.execute("INSERT OR REPLACE INTO narrations VALUES (?,?,?,?,?)",
               (h, kind, model, json.dumps(data), datetime.now(UTC).isoformat()))
    db.commit()


def complete_with_fallback(complete: Callable, roles: tuple[str, ...], prompt: str, system: str, schema: dict):
    """Try each role in order; return (result, role). Raises the last error if all fail."""
    last: Exception | None = None
    for role in roles:
        try:
            return complete(role, prompt, system=system, json_schema=schema), role
        except (LLMError, Exception) as e:  # noqa: BLE001 - a role being unavailable is expected (no key)
            last = e
    raise LLMError(f"no usable model for roles {roles}: {last}")


def _item_block(items: list[dict], limit: int, summary_chars: int = 320) -> str:
    out = []
    for i in items[:limit]:
        summary = (i.get("summary") or "")[:summary_chars]
        out.append(f"[{i['id']}] {i['date']} ({i['source']}) {i['title']}\n{summary}")
    return "\n\n".join(out)


def _writer(complete: Callable | None, role: str) -> tuple[Callable, str | None]:
    """The completion function and the role's preferred candidate (None for an injected test function).

    Cached prose is keyed by the preferred candidate and stored only when that candidate wrote it, so switching
    models regenerates the text and a fallback answer (e.g. local, because a key is missing) is never frozen.
    """
    if complete is not None:
        return complete, None
    from ..llm import LLMRouter

    router = LLMRouter.from_config(job="site")
    try:
        preferred = router.candidates(role)[0][0]
    except Exception:  # noqa: BLE001
        preferred = None
    return router.complete, preferred


def theme_prompt(theme: Theme, lens: str) -> tuple[str, str]:
    system = ("You write one card of a market-intelligence briefing. Use only the items below; do not add outside "
              f"facts. {STYLE}")
    prompt = f"""Entities that tie these items together: {", ".join(theme.entities)}.

Items:
{_item_block(theme.items, 10)}

Return:
- title: the theme in at most 8 words, specific (name the companies or products), no colon.
- narrative: exactly two sentences on what happened across these items.
- so_what: one sentence on why it matters for this reader: {lens}
  If it barely matters for this reader, say so plainly.
- cited_ids: the ids of the items you used."""
    return system, prompt


def parse_theme(data: dict, theme: Theme) -> dict:
    return {"title": _clean(data.get("title", "")) or theme.seed, "narrative": _clean(data.get("narrative", "")),
            "so_what": _clean(data.get("so_what", "")),
            "cited_ids": [c for c in data.get("cited_ids", []) if c in {i["id"] for i in theme.items}]}


def narrate_theme(db: sqlite3.Connection, theme: Theme, lens: str, complete: Callable | None = None,
                  roles: tuple[str, ...] = ("writing", "extraction")) -> dict:
    """Title, two-sentence narrative and a so-what line for a theme, grounded in its items."""
    complete, preferred = _writer(complete, roles[0])
    h = _hash("theme-v1", theme.kind, theme.entities, [i["id"] for i in theme.items[:10]], lens, preferred)
    hit = _cached(db, h)
    if hit:
        return hit
    system, prompt = theme_prompt(theme, lens)
    try:
        res, role = complete_with_fallback(complete, roles, prompt, system, THEME_SCHEMA)
        out = {**parse_theme(res.data or {}, theme), "role": role}
        model = f"{res.provider}/{res.model}"
        served = getattr(res, "candidate", None)
        if preferred is None or served == preferred:
            _store(db, h, "theme", model, out)
    except LLMError as e:
        out = {"title": " · ".join(theme.entities[:3]), "narrative": "", "so_what": "", "cited_ids": [],
               "role": None, "error": str(e)[:200]}
        model = "none"
    return {"model": model, **out}


# ---------------------------------------------------------------------------------------------- Ask


def retrieve(db: sqlite3.Connection, question: str, days: int = 90, limit: int = 12, vector: bool = True) -> dict:
    """Evidence for a question: entities named in it (with their trend) and the best-matching items.

    Hybrid retrieval (ADR-0019): keyword search (SQLite FTS5, BM25) and vector search (Neo4j index) each rank the
    items; reciprocal rank fusion merges the two rankings. Keywords hold on to specific words ("insurance",
    "bank"), vectors match meaning ("EU" ~ "AI Office", "Commission"). Without vectors it is keyword-only."""
    from ..signals import trend_metrics

    end = date.fromisoformat(db.execute("SELECT MAX(date) FROM items").fetchone()[0])
    start = (end - timedelta(days=days)).isoformat()
    def in_question(name: str) -> bool:  # whole words only: "AI Mode" must not match "AI model"
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", question, re.IGNORECASE) is not None

    ents = [r for r in db.execute("SELECT name, mentions FROM entities WHERE mentions >= 3 ORDER BY mentions DESC")
            if len(r[0]) >= 3 and in_question(r[0])][:4]
    trends = [t for t in (trend_metrics(name, db, 14, periods=6) for name, _ in ents) if t]
    terms = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]{2,}", question) if w.lower() not in _STOP]
    keyword: list[str] = []
    if terms:
        q = " OR ".join(f'"{t}"' for t in terms)
        keyword = [iid for iid, _ in db.execute(
            "SELECT id, bm25(items_fts) FROM items_fts WHERE items_fts MATCH ? ORDER BY rank LIMIT 60", (q,))]
    semantic: list[str] = []
    if vector:
        from ..vectors import item_ranking

        semantic = item_ranking(question, k=40)
    fused = rrf([keyword, semantic])
    # Scaled so an item ranked first by one method scores about 1, first by both about 2.
    items: dict[str, dict] = {iid: {"text_score": 61 * score} for iid, score in fused.items()}
    for t in trends:
        for ev in t["evidence"]:
            items.setdefault(ev["id"], {"text_score": 0.0})
    rows = []
    if items:
        for r in db.execute(f"""SELECT id, date, source, title, url, relevance, summary, sources_count FROM items
                WHERE id IN ({','.join('?' * len(items))}) AND date >= ?""", [*items, start]):
            s = items[r[0]]["text_score"] + 0.15 * (r[5] or 0) + 0.1 * (r[7] - 1)  # relevance and coverage break ties
            rows.append(({"id": r[0], "date": r[1], "source": r[2], "title": r[3], "url": r[4], "relevance": r[5],
                          "summary": r[6]}, s))
    rows.sort(key=lambda t: t[1], reverse=True)
    return {"entities": [e for e, _ in ents], "trends": trends, "items": [r for r, _ in rows[:limit]],
            "terms": terms, "window": [start, end.isoformat()], "vector": bool(semantic)}


_STOP = {"what", "which", "who", "how", "why", "the", "and", "for", "are", "was", "were", "about", "with", "this",
         "that", "these", "those", "from", "into", "does", "did", "has", "have", "been", "should", "could", "would",
         "will", "can", "there", "their", "they", "them", "than", "then", "most", "more", "last", "week", "weeks",
         "month", "months", "days", "news", "any", "all", "our", "your", "you", "tell", "happened", "happening",
         "around", "recently", "recent", "latest", "going", "trend", "trends", "moving", "doing", "being", "between",
         "show", "give", "explain", "summarise", "summarize", "important", "matters"}


def answer_prompt(question: str, lens: str, ev: dict) -> tuple[str, str]:
    trend_lines = "\n".join(
        f"- {t['entity']}: mentions per 2-week window (oldest first) {[p['mentions'] for p in t['series']]}, "
        f"first seen {t['first_seen']}" for t in ev["trends"])
    system = ("You answer questions about AI and tech market signals using only the evidence provided: news items "
              "with ids and mention counts. Cite item ids in square brackets after the claims they support. If the "
              f"evidence does not answer the question, say so and say what is missing. {STYLE}")
    prompt = f"""Question: {question}

Reader: {lens}

Trend data:
{trend_lines or "- none"}

Items:
{_item_block(ev['items'], 12, 400)}

Answer in at most 150 words."""
    return system, prompt


def parse_answer(data: dict, ev: dict) -> dict:
    known = {i["id"]: i for i in ev["items"]}
    cited = [known[c] for c in data.get("cited_ids", []) if c in known]
    text = re.sub(r"\[([0-9a-f]{16})\]", lambda m: f"[{m.group(1)}]" if m.group(1) in known else "", data.get("answer", ""))
    return {"answer": _clean(text), "cited": [{k: c[k] for k in ("id", "date", "source", "title", "url")} for c in cited],
            "evidence_is_thin": bool(data.get("evidence_is_thin"))}


def answer(db: sqlite3.Connection, question: str, lens: str, complete: Callable | None = None,
           roles: tuple[str, ...] = ("reasoning", "extraction")) -> dict:
    ev = retrieve(db, question)
    trace = [f"matched entities: {', '.join(ev['entities']) or 'none'}",
             f"full-text terms: {', '.join(ev['terms']) or 'none'}",
             f"read {len(ev['items'])} items from {ev['window'][0]} to {ev['window'][1]}"]
    if not ev["items"]:
        return {"answer": "I found no items in the corpus that match this question. Try naming a company, product or "
                          "topic as it would appear in a headline.", "cited": [], "evidence_is_thin": True,
                "trace": trace, "model": None}
    complete, preferred = _writer(complete, roles[0])
    system, prompt = answer_prompt(question, lens, ev)
    h = _hash("answer-v1", prompt, preferred)
    hit = _cached(db, h)
    if hit:
        return {**hit, "trace": trace + ["(cached answer)"]}
    try:
        res, role = complete_with_fallback(complete, roles, prompt, system, ANSWER_SCHEMA)
        out = {**parse_answer(res.data or {}, ev), "role": role}
        model = f"{res.provider}/{res.model}"
    except LLMError as e:
        return {"answer": f"No model available to answer ({e}).", "cited": [], "evidence_is_thin": True,
                "trace": trace, "model": None}
    if preferred is None or getattr(res, "candidate", None) == preferred:
        _store(db, h, "answer", model, out)
    return {"model": model, **out, "trace": trace + [f"answered by {model} (role {role})"]}
