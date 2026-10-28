"""The signal store: entities, mentions, relations and the trend maths Graqle doesn't do.

`rebuild()` derives every table from the corpus + stored extractions, deterministically and without LLM calls:
- canonicalise entity names (normalised key + config/aliases.yaml), majority type and most common spelling
- drop generic entities ("AI", "LLM") and entities not actually named in the item (the model sometimes copies
  the reader's interests from the prompt into the entity list)
- keep only relations whose types fit the relation (RELEASED_BY must be product -> company, ...), flipping ones
  that are simply reversed

Metrics compare a current window with the average of the previous `baseline_periods` windows of equal size.
Mover score = (current - baseline) / sqrt(baseline + 1): a rough Poisson z-score, so going 0 -> 3 mentions
ranks below 10 -> 30. Confidence labels say how much evidence there is; they are not statistical claims.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml

from .config import CONFIG_DIR
from .corpus import Corpus, _summary_from_body
from .extract import RELATION_RULES, SIGNALS_DB, connect

STOP_KEYS = {
    "ai", "artificialintelligence", "genai", "generativeai", "llm", "llms", "model", "models", "aimodel",
    "aimodels", "users", "user", "startup", "startups", "company", "companies", "technology", "software",
    "data", "tldr", "tldrai", "alphasignal", "newsletter", "developers", "developer", "internet", "app", "apps",
    "investors", "investor", "founders", "founder", "subscribers", "customers", "researchers", "engineers",
    "enterprises", "enterprise", "coding", "code", "api", "apis", "cloud", "opensource", "ceo", "vc", "vcs",
}


def entity_key(name: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"['’]s\b", "", n)
    n = re.sub(r"[,]?\s+(inc|corp|corporation|ltd|llc|plc|gmbh|co)\.?$", "", n)
    return re.sub(r"[^a-z0-9+#.]", "", n).strip(".")


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


_SUFFIXES = ("ations", "ation", "ings", "ing", "ers", "er", "ies", "es", "s", "ed", "al")


def _stem(word: str) -> str:
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)]
    return word


def named_in(name: str, text: str, compact_text: str | None = None) -> bool:
    """True if the entity is actually named in the item: whole name, or most of its words."""
    compact_text = compact_text if compact_text is not None else _compact(text)
    if _compact(name) and _compact(name) in compact_text:
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 3 or w.isdigit()]
    if not words:
        return False
    hay = {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())}
    return sum(_stem(w) in hay for w in words) / len(words) >= 0.6


@dataclass
class AliasTable:
    keys: dict[str, str] = field(default_factory=dict)          # any spelling's key -> canonical key
    names: dict[str, list[str]] = field(default_factory=dict)   # canonical key -> all spellings
    display: dict[str, str] = field(default_factory=dict)       # canonical key -> canonical name

    def get(self, key: str, default: str) -> str:
        return self.keys.get(key, default)


def load_aliases(path: Path | None = None) -> AliasTable:
    """From config/aliases.yaml ({canonical name: [aliases]})."""
    path = path if path is not None else CONFIG_DIR / "aliases.yaml"
    table = AliasTable()
    if not path.exists():
        return table
    for canonical, aliases in (yaml.safe_load(path.read_text()) or {}).items():
        ck = entity_key(canonical)
        table.display[ck] = canonical
        table.names[ck] = [canonical, *(aliases or [])]
        for spelling in table.names[ck]:
            table.keys[entity_key(spelling)] = ck
    return table


BRANDS = ("claude", "gpt", "openai", "gemini", "google", "anthropic", "meta", "llama", "microsoft", "apple",
          "amazon", "aws", "nvidia", "mistral", "qwen", "deepseek", "grok", "xai")


def merge_brand_prefixes(keys: set[str]) -> dict[str, str]:
    """Merge obvious variants when both exist: plurals ("mcpservers" -> "mcpserver") and versioned short names
    -> the brand-prefixed entity ("opus5.5" -> "claudeopus5.5")."""
    out = {}
    for k in keys:
        if k.endswith("s") and len(k) > 4 and k[:-1] in keys:
            out[k] = k[:-1]  # plural -> singular ("mcpservers" -> "mcpserver")
            continue
        if not any(ch.isdigit() for ch in k):
            continue  # brand merge only for versioned names; "code" must not become "claudecode"
        targets = [b + k for b in BRANDS if b + k in keys]
        if len(targets) == 1:
            out[k] = targets[0]
    return out


def _family(source_id: str) -> str:
    """Independent curator: all TL;DR editions are one family, AlphaSignal another."""
    return source_id.split("_", 1)[0]


SCHEMA = """
DROP TABLE IF EXISTS items; DROP TABLE IF EXISTS entities; DROP TABLE IF EXISTS mentions; DROP TABLE IF EXISTS relations;
DROP TABLE IF EXISTS items_fts;
CREATE TABLE items (id TEXT PRIMARY KEY, date TEXT, source TEXT, family TEXT, edition TEXT, title TEXT, url TEXT,
  section TEXT, category TEXT, secondary_category TEXT, relevance INTEGER, relevance_reason TEXT,
  sources_count INTEGER, families TEXT, engagement TEXT, extracted INTEGER, summary TEXT);
CREATE VIRTUAL TABLE items_fts USING fts5(id UNINDEXED, title, summary, tokenize = 'porter unicode61');
CREATE TABLE entities (id INTEGER PRIMARY KEY, key TEXT UNIQUE, name TEXT, type TEXT, first_seen TEXT, mentions INTEGER);
CREATE TABLE mentions (entity_id INTEGER, item_id TEXT, date TEXT, source TEXT, family TEXT, PRIMARY KEY (entity_id, item_id));
CREATE TABLE relations (src_id INTEGER, relation TEXT, dst_id INTEGER, item_id TEXT, date TEXT);
CREATE INDEX mentions_date ON mentions(date); CREATE INDEX items_date ON items(date);
"""


@dataclass
class RebuildStats:
    items: int = 0
    extracted: int = 0
    entities: int = 0
    mentions: int = 0
    relations: int = 0
    dropped_generic: int = 0
    dropped_not_named: int = 0
    dropped_relations: int = 0
    flipped_relations: int = 0


def rebuild(corpus: Corpus | None = None, db: sqlite3.Connection | None = None,
            aliases: AliasTable | None = None, exclude_sources: frozenset[str] = frozenset()) -> RebuildStats:
    """`exclude_sources`: source ids left out entirely, as if never ingested (the public store, ADR-0015)."""
    corpus = corpus if corpus is not None else Corpus()
    db = db if db is not None else connect()
    aliases = load_aliases() if aliases is None else aliases
    extractions = {r[0]: json.loads(r[1]) for r in db.execute("SELECT item_id, data FROM extractions")}
    db.executescript(SCHEMA)
    st = RebuildStats()

    surface: dict[str, Counter] = defaultdict(Counter)
    types: dict[str, Counter] = defaultdict(Counter)
    first: dict[str, str] = {}
    item_ents: dict[str, list[str]] = {}
    item_rows, rel_rows = [], []
    item_info: dict[str, tuple[str, str]] = {}

    for meta, body in corpus.iter_meta():
        if meta["source"] in exclude_sources:
            continue
        iid, day = meta["id"], str(meta["date"])
        also = [a for a in meta.get("also_in") or [] if a["source"] not in exclude_sources]
        fams = sorted({_family(meta["source"])} | {_family(a["source"]) for a in also})
        ex = extractions.get(iid)
        st.items += 1
        item_rows.append((
            iid, day, meta["source"], _family(meta["source"]), meta.get("edition"), meta["title"], meta["url"],
            meta.get("section"), ex and ex.get("category"), ex and ex.get("secondary_category"),
            ex and ex.get("relevance"), ex and ex.get("relevance_reason"), 1 + len(also), ",".join(fams),
            json.dumps(meta.get("engagement")) if meta.get("engagement") else None, 1 if ex else 0,
            _summary_from_body(body),
        ))
        item_info[iid] = (day, meta["source"])
        if not ex:
            continue
        st.extracted += 1
        text = f"{meta['title']}\n{body}"
        compact = _compact(text)
        keys, local_type = [], {}
        for e in ex.get("entities", []):
            name = (e.get("name") or "").strip()
            k = aliases.get(entity_key(name), entity_key(name))
            if not k or k in STOP_KEYS or len(k) < 2:
                st.dropped_generic += 1
                continue
            # Named in the item under this spelling or any known alias ("MCP" for "Model Context Protocol").
            if not any(named_in(n, text, compact) for n in [name, *aliases.names.get(k, [])]):
                st.dropped_not_named += 1
                continue
            if k in keys:
                continue
            keys.append(k)
            surface[k][name] += 1
            types[k][e.get("type")] += 1
            local_type[entity_key(name)] = (k, e.get("type"))
            first[k] = min(first.get(k, day), day)
        item_ents[iid] = keys
        for r in ex.get("relations", []):
            s = local_type.get(entity_key(r.get("source", "")))
            t = local_type.get(entity_key(r.get("target", "")))
            rule = RELATION_RULES.get(r.get("relation"))
            if not (s and t and rule) or s[0] == t[0]:
                st.dropped_relations += 1
                continue
            src_ok, dst_ok = rule[0], rule[1]
            if s[1] in src_ok and t[1] in dst_ok:
                rel_rows.append((s[0], r["relation"], t[0], iid, day))
            elif t[1] in src_ok and s[1] in dst_ok:
                rel_rows.append((t[0], r["relation"], s[0], iid, day))
                st.flipped_relations += 1
            else:
                st.dropped_relations += 1

    merges = merge_brand_prefixes(set(surface))
    for short, full in merges.items():
        surface[full].update(surface.pop(short))
        types[full].update(types.pop(short))
        first[full] = min(first[full], first.pop(short))
    if merges:
        item_ents = {iid: list(dict.fromkeys(merges.get(k, k) for k in keys)) for iid, keys in item_ents.items()}
        rel_rows = [(merges.get(a, a), r, merges.get(b, b), iid, d) for a, r, b, iid, d in rel_rows if merges.get(a, a) != merges.get(b, b)]

    db.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", item_rows)
    db.executemany("INSERT INTO items_fts VALUES (?,?,?)", [(r[0], r[5], r[16]) for r in item_rows])
    ids: dict[str, int] = {}
    counts = Counter(k for keys in item_ents.values() for k in keys)
    for i, k in enumerate(sorted(surface), 1):
        ids[k] = i
        db.execute("INSERT INTO entities VALUES (?,?,?,?,?,?)",
                   (i, k, aliases.display.get(k) or surface[k].most_common(1)[0][0], types[k].most_common(1)[0][0],
                    first[k], counts[k]))
    for iid, keys in item_ents.items():
        day, source = item_info[iid]
        for k in keys:
            db.execute("INSERT OR IGNORE INTO mentions VALUES (?,?,?,?,?)", (ids[k], iid, day, source, _family(source)))
    db.executemany("INSERT INTO relations VALUES (?,?,?,?,?)", [(ids[s], r, ids[t], iid, d) for s, r, t, iid, d in rel_rows])
    db.commit()
    st.entities, st.relations = len(ids), len(rel_rows)
    st.mentions = db.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    return st


PUBLIC_DB = SIGNALS_DB.with_name("signals_public.sqlite")


def public_store(exclude_sources: frozenset[str], corpus: Corpus | None = None, main: Path | None = None,
                 path: Path | None = None) -> sqlite3.Connection:
    """The store the public edition is built from: the same corpus without the excluded sources (ADR-0015).

    Separate file, so every count, theme, entity page and brief signal is computed without those sources, not
    filtered afterwards. Extractions and cached prose are copied from the main store (no model calls for
    extraction); votes are not copied: they are private.
    """
    path = path or PUBLIC_DB
    db = connect(path)
    db.execute("ATTACH DATABASE ? AS main_store", (str(main or SIGNALS_DB),))
    db.execute("DELETE FROM extractions")
    db.execute("INSERT INTO extractions SELECT * FROM main_store.extractions")
    db.execute("INSERT OR IGNORE INTO narrations SELECT * FROM main_store.narrations")
    db.commit()
    db.execute("DETACH DATABASE main_store")
    rebuild(corpus, db, exclude_sources=frozenset(exclude_sources))
    return db


# ---------------------------------------------------------------------- metrics


@dataclass
class Window:
    as_of: date
    days: int
    periods: int

    @property
    def start(self) -> date:
        return self.as_of - timedelta(days=self.days - 1)

    @property
    def base_start(self) -> date:
        return self.start - timedelta(days=self.days * self.periods)


def as_of_default(db: sqlite3.Connection) -> date:
    d = db.execute("SELECT MAX(date) FROM mentions").fetchone()[0] or db.execute("SELECT MAX(date) FROM items").fetchone()[0]
    return date.fromisoformat(d) if d else date.today()


def _confidence(current: int, families: int) -> str:
    if current >= 8 and families >= 2:
        return "high"
    if current >= 4:
        return "medium"
    return "low"


def _evidence(db: sqlite3.Connection, entity_id: int, start: date, end: date, n: int = 5) -> list[dict]:
    rows = db.execute("""SELECT i.id, i.date, i.source, i.title, i.url, i.relevance FROM mentions m JOIN items i ON i.id = m.item_id
        WHERE m.entity_id = ? AND m.date BETWEEN ? AND ? ORDER BY COALESCE(i.relevance, 0) DESC, i.sources_count DESC, i.date DESC LIMIT ?""",
                      (entity_id, start.isoformat(), end.isoformat(), n)).fetchall()
    return [{"id": r[0], "date": r[1], "source": r[2], "title": r[3], "url": r[4], "relevance": r[5]} for r in rows]


@dataclass
class Mover:
    entity: str
    type: str
    current: int
    baseline: float
    score: float
    families: int
    first_seen: str
    new: bool
    confidence: str
    evidence: list[dict] = field(default_factory=list)


def top_movers(db: sqlite3.Connection | None = None, window_days: int = 7, entity_type: str | None = None,
               limit: int = 15, min_current: int = 3, periods: int = 4, as_of: date | None = None,
               direction: str = "up", min_relevance: int = 0) -> list[Mover]:
    """Entities whose mention count in the last `window_days` moved most against the previous `periods` windows.

    min_relevance > 0 counts only mentions in items at least that relevant to the lens (cuts off-topic movers).
    """
    db = db if db is not None else connect()
    w = Window(as_of or as_of_default(db), window_days, periods)
    q = """SELECT e.id, e.name, e.type, e.first_seen,
          SUM(m.date >= :start) AS cur,
          SUM(m.date < :start) AS base,
          COUNT(DISTINCT CASE WHEN m.date >= :start THEN m.family END) AS fams
        FROM mentions m JOIN entities e ON e.id = m.entity_id JOIN items i ON i.id = m.item_id
        WHERE m.date BETWEEN :base_start AND :as_of AND COALESCE(i.relevance, 0) >= :minrel {type_filter}
        GROUP BY e.id"""
    params = {"start": w.start.isoformat(), "base_start": w.base_start.isoformat(), "as_of": w.as_of.isoformat(),
              "minrel": min_relevance}
    type_filter = ""
    if entity_type:
        type_filter, params["etype"] = "AND e.type = :etype", entity_type
    out: list[tuple[Mover, int]] = []
    for eid, name, etype, first_seen, cur, base, fams in db.execute(q.format(type_filter=type_filter), params):
        base_mean = (base or 0) / periods
        score = (cur - base_mean) / math.sqrt(base_mean + 1)
        if direction == "up" and (cur < min_current or score <= 0):
            continue
        if direction == "down" and (base_mean < min_current or score >= 0):
            continue
        out.append((Mover(name, etype, cur, round(base_mean, 2), round(score, 2), fams, first_seen,
                          first_seen >= w.start.isoformat(), _confidence(cur, fams)), eid))
    out.sort(key=lambda t: t[0].score, reverse=(direction == "up"))
    lo, hi = (w.start, w.as_of) if direction == "up" else (w.base_start, w.start - timedelta(days=1))
    for m, eid in out[:limit]:
        m.evidence = _evidence(db, eid, lo, hi, 3)
    return [m for m, _ in out[:limit]]


def find_entity(db: sqlite3.Connection, name: str) -> tuple | None:
    k = load_aliases().get(entity_key(name), entity_key(name))
    row = db.execute("SELECT id, name, type, first_seen, mentions FROM entities WHERE key = ?", (k,)).fetchone()
    if row:
        return row
    return db.execute("SELECT id, name, type, first_seen, mentions FROM entities WHERE key LIKE ? ORDER BY mentions DESC LIMIT 1",
                      (f"%{k}%",)).fetchone()


def trend_metrics(name: str, db: sqlite3.Connection | None = None, window_days: int = 7, periods: int = 8,
                  as_of: date | None = None) -> dict | None:
    """Mention series for one entity: counts per window (oldest first), velocity, novelty, related entities."""
    db = db if db is not None else connect()
    row = find_entity(db, name)
    if row is None:
        return None
    eid, ename, etype, first_seen, total = row
    end = as_of or as_of_default(db)
    series = []
    for p in range(periods - 1, -1, -1):
        hi = end - timedelta(days=window_days * p)
        lo = hi - timedelta(days=window_days - 1)
        n = db.execute("SELECT COUNT(*) FROM mentions WHERE entity_id = ? AND date BETWEEN ? AND ?",
                       (eid, lo.isoformat(), hi.isoformat())).fetchone()[0]
        series.append({"start": lo.isoformat(), "end": hi.isoformat(), "mentions": n})
    cur, prev = series[-1]["mentions"], series[-2]["mentions"] if len(series) > 1 else 0
    start = end - timedelta(days=window_days - 1)
    related = db.execute("""SELECT e.name, e.type, COUNT(*) c FROM mentions a JOIN mentions b ON a.item_id = b.item_id AND b.entity_id != a.entity_id
        JOIN entities e ON e.id = b.entity_id WHERE a.entity_id = ? AND a.date >= ? GROUP BY e.id ORDER BY c DESC LIMIT 8""",
                         (eid, (end - timedelta(days=window_days * periods)).isoformat())).fetchall()
    rels = db.execute("""SELECT s.name, r.relation, t.name, COUNT(*) c FROM relations r JOIN entities s ON s.id = r.src_id
        JOIN entities t ON t.id = r.dst_id WHERE r.src_id = ? OR r.dst_id = ? GROUP BY 1,2,3 ORDER BY c DESC LIMIT 8""", (eid, eid)).fetchall()
    return {
        "entity": ename, "type": etype, "first_seen": first_seen, "total_mentions": total,
        "window_days": window_days, "series": series,
        "velocity": round((cur - prev) / max(prev, 1), 2), "new_in_window": first_seen >= start.isoformat(),
        "related": [{"entity": r[0], "type": r[1], "co_mentions": r[2]} for r in related],
        "relations": [{"source": r[0], "relation": r[1], "target": r[2], "count": r[3]} for r in rels],
        "evidence": _evidence(db, eid, start, end, 5),
    }


def cooccurrence_growth(db: sqlite3.Connection | None = None, window_days: int = 14, periods: int = 3,
                        limit: int = 10, min_current: int = 2, as_of: date | None = None) -> list[dict]:
    """Entity pairs mentioned together more often now than before: clusters forming."""
    db = db if db is not None else connect()
    w = Window(as_of or as_of_default(db), window_days, periods)
    rows = db.execute("""SELECT a.entity_id, b.entity_id, SUM(a.date >= :start) cur, SUM(a.date < :start) base
        FROM mentions a JOIN mentions b ON a.item_id = b.item_id AND a.entity_id < b.entity_id
        WHERE a.date BETWEEN :base_start AND :as_of GROUP BY 1, 2 HAVING cur >= :min""",
                      {"start": w.start.isoformat(), "base_start": w.base_start.isoformat(), "as_of": w.as_of.isoformat(),
                       "min": min_current}).fetchall()
    names = dict(db.execute("SELECT id, name FROM entities").fetchall())
    out = []
    for a, b, cur, base in rows:
        base_mean = base / periods
        score = (cur - base_mean) / math.sqrt(base_mean + 1)
        if score > 0:
            out.append({"pair": [names[a], names[b]], "current": cur, "baseline": round(base_mean, 2), "score": round(score, 2)})
    return sorted(out, key=lambda r: r["score"], reverse=True)[:limit]


MIN_VOTES = 3  # below this, votes are stored but do not re-rank (one click must not swing the list)


def affinity(db: sqlite3.Connection) -> tuple[dict[str, float], dict[str, float], int]:
    """What the user's +/- votes say: per entity and per category, squashed to -1..1 (a / (1 + |a|))."""
    votes = db.execute("SELECT item_id, vote FROM feedback").fetchall()
    ent: dict[str, float] = {}
    cat: dict[str, float] = {}
    for iid, v in votes:
        for (name,) in db.execute("SELECT e.name FROM mentions m JOIN entities e ON e.id = m.entity_id WHERE m.item_id = ?", (iid,)):
            ent[name] = ent.get(name, 0) + v
        row = db.execute("SELECT category FROM items WHERE id = ?", (iid,)).fetchone()
        if row and row[0]:
            cat[row[0]] = cat.get(row[0], 0) + v
    squash = lambda a: a / (1 + abs(a))  # noqa: E731
    return {k: squash(v) for k, v in ent.items() if v}, {k: squash(v) for k, v in cat.items() if v}, len(votes)


def reading_list(db: sqlite3.Connection | None = None, days: int = 7, limit: int = 10, as_of: date | None = None,
                 category: str | None = None, personalise: bool = True) -> list[dict]:
    """Items worth reading: relevance to the lens first, then corroboration, entity momentum and (after a few
    votes) what the user liked or dismissed before. Every component is returned so the ranking can be explained."""
    db = db if db is not None else connect()
    end = as_of or as_of_default(db)
    start = end - timedelta(days=days - 1)
    momentum = {m.entity: m.score for m in top_movers(db, window_days=days, limit=200, min_current=2, as_of=end)}
    ent_aff, cat_aff, n_votes = affinity(db) if personalise else ({}, {}, 0)
    use_votes = n_votes >= MIN_VOTES
    q = """SELECT id, date, source, title, url, category, relevance, relevance_reason, sources_count, families, engagement
           FROM items WHERE date BETWEEN ? AND ? AND extracted = 1"""
    args: list = [start.isoformat(), end.isoformat()]
    if category:
        q += " AND (category = ? OR secondary_category = ?)"
        args += [category, category]
    out = []
    for iid, d, src, title, url, cat, rel, reason, nsrc, fams, eng in db.execute(q, args):
        ents = [r[0] for r in db.execute("SELECT e.name FROM mentions m JOIN entities e ON e.id = m.entity_id WHERE m.item_id = ?", (iid,))]
        mom = max((momentum.get(e, 0) for e in ents), default=0)
        likes = max((json.loads(eng) or {}).values(), default=0) if eng else 0
        score = (rel or 0) * 1.0 + 0.6 * (len(fams.split(",")) - 1) + 0.25 * (nsrc - 1) + 0.3 * min(mom, 3) \
            + 0.15 * math.log10(1 + likes)
        personal, because = 0.0, ""
        if use_votes:
            liked = sorted(((ent_aff[e], e) for e in ents if e in ent_aff), reverse=True)
            e_part = sum(a for a, _ in liked) / max(len(liked), 1)
            personal = 0.5 * e_part + 0.3 * cat_aff.get(cat, 0)  # at most +-0.8: relevance still dominates
            pos = [e for a, e in liked if a > 0][:2]
            neg = [e for a, e in liked if a < 0][:2]
            because = (f"you liked items about {', '.join(pos)}" if personal > 0.05 and pos else
                       f"similar to items you dismissed ({', '.join(neg)})" if personal < -0.05 and neg else "")
        score += personal
        out.append({"id": iid, "date": d, "source": src, "title": title, "url": url, "category": cat,
                    "relevance": rel, "why": reason, "covered_by": fams.split(","), "entities": ents[:5],
                    "personal": round(personal, 2), "because": because, "score": round(score, 2)})
    ranked, kept_titles = [], []
    for r in sorted(out, key=lambda r: r["score"], reverse=True):
        words = _title_words(r["title"])
        # The same story in two editions has different URLs and headlines; keep the higher-ranked one.
        if any(_jaccard(words, w) >= 0.5 and abs((date.fromisoformat(r["date"]) - d).days) <= 3 for w, d in kept_titles):
            continue
        kept_titles.append((words, date.fromisoformat(r["date"])))
        ranked.append(r)
        if len(ranked) == limit:
            break
    return ranked


_TITLE_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "its", "is", "as", "by", "at", "new"}


def _title_words(title: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _TITLE_STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def category_counts(db: sqlite3.Connection | None = None, days: int = 28, as_of: date | None = None) -> list[dict]:
    db = db if db is not None else connect()
    end = as_of or as_of_default(db)
    half = end - timedelta(days=days // 2)
    rows = db.execute("""SELECT category, COUNT(*), SUM(date > ?), ROUND(AVG(relevance), 2) FROM items
        WHERE extracted = 1 AND date BETWEEN ? AND ? GROUP BY category ORDER BY 2 DESC""",
                      (half.isoformat(), (end - timedelta(days=days - 1)).isoformat(), end.isoformat())).fetchall()
    return [{"category": c, "items": n, "recent_half": r, "avg_relevance": a} for c, n, r, a in rows]
