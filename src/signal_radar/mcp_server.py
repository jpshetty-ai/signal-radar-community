"""signal-sources-mcp: the MCP server Claude (and scheduled jobs) use to reach Signal Radar's sources.

Design notes, since this is also a worked example of building an MCP server:

- **Tools return typed models.** MCPServer derives each tool's input schema from the type hints and its output
  schema from the return annotation, and sends both `structuredContent` (JSON a program can rely on) and a text
  rendering (what a model reads). Docstrings become the tool descriptions, so they are written for the model.
- **Expected failures raise ToolError.** The SDK passes a ToolError's message to the client with `isError: true`.
  Anything else is treated as a crash and the client only sees "Error executing tool X", so the
  "you need to sign in" message must be a ToolError or the model can't tell you what to do.
- **Annotations are hints, not security.** `readOnlyHint` lets a client skip the confirmation prompt for
  fetches; `destructiveHint` on unsubscribe tells it to ask. The server still enforces what it allows.
- **stdout belongs to the protocol.** On stdio transport any stray print() corrupts the JSON-RPC stream, so all
  logging goes to stderr.
- **Sync tools are fine.** MCPServer runs sync tool functions in a worker thread, so blocking HTTP calls to
  Graph don't stall the event loop.
- **Resources vs tools.** Corpus items are *resources* (`corpus://items/{id}`, `corpus://digest/{date}`): data the
  client can read and attach as context, addressed by URI, with no side effects. Anything that computes, fetches
  or writes is a *tool*.
- **Prompts are user-chosen templates.** Tools are called by the model on its own; resources are data a client can
  attach; prompts (`weekly_signal_brief`, `learning_radar`, `content_ideas`) are templates the *user* picks (Claude
  Code lists them as slash commands). A prompt returns messages that set up a task and name the tools to use; it
  computes nothing itself.
- **Dependencies are injectable.** `build_server(mail_factory=...)` lets tests swap the mailbox for a fake and
  drive the server in-process with `mcp.Client(server)`.
"""

from __future__ import annotations

import logging
import sys
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from .config import CONFIG_DIR, PROJECT_ROOT, EmailNewsletterSource, FeedSource, load_sources, save_sources
from .items import Item
from .sources import (
    FeedError, MailError, MailFactory, SourceNotSupported, build_adapter, default_mail_factory, sender_groups,
)

log = logging.getLogger("signal_sources_mcp")

INSTRUCTIONS = """\
Signal Radar: AI and tech market intelligence from the user's newsletters.
- What is moving: top_movers (rising or fading entities), emerging_clusters (entities newly co-mentioned),
  trend_metrics (one entity's weekly series, related entities, evidence), reading_list (items worth reading,
  ranked for the user's lens: financial services, risk, compliance, EU AI Act, enterprise architecture).
- Evidence: every signal carries item ids; read corpus://items/{id} for the item, corpus://digest/{date} for a day.
- Sources: list_sources, fetch_source (read-only preview), run_ingest (writes new items to the corpus),
  suggest_newsletter_filter, subscribe_source, unsubscribe_source.
Never present a signal without its evidence and confidence; say when evidence is thin. If a tool says a mail
account needs a sign-in, tell the user to run `signal-radar auth <account>` in a terminal.
"""


# ------------------------------------------------------------------ result models


class SourceInfo(BaseModel):
    id: str
    type: str
    enabled: bool
    tags: list[str]
    detail: str = Field(description="account/folder for newsletters, URL for feeds")


class FetchResult(BaseModel):
    source_id: str
    since: datetime
    scanned: int = Field(description="emails or entries looked at")
    matched: int = Field(description="of those, how many belong to this source")
    item_count: int
    items: list[Item]
    skipped: list[str] = Field(default_factory=list, description="matched emails that are deliberately not news")
    deferred: int = Field(0, description="matched issues to retry later (e.g. link resolution failed)")
    warnings: list[str]


class SenderGroup(BaseModel):
    from_address: str
    from_name: str
    count: int
    latest: str
    sample_subjects: list[str]
    proposed_match: dict[str, list[str]]


class SubscriptionChange(BaseModel):
    source_id: str
    action: Literal["subscribed", "enabled", "disabled", "removed"]
    note: str = ""


def _describe(s) -> str:
    if isinstance(s, EmailNewsletterSource):
        return f"{s.account}:{s.folder} ({s.parser} parser)"
    return str(getattr(s, "url", None) or getattr(s, "query", None) or getattr(s, "repos", ""))


def build_server(mail_factory: MailFactory = default_mail_factory, sources_path: Path | None = None,
                 corpus_root: Path | None = None, signals_db: Path | None = None) -> MCPServer:
    sources_path = sources_path or CONFIG_DIR / "sources.yaml"
    server = MCPServer("signal-sources", instructions=INSTRUCTIONS, version="0.2.0")
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def list_sources(include_disabled: bool = True) -> list[SourceInfo]:
        """List the subscribed sources (newsletters and feeds) from config/sources.yaml."""
        cfg = load_sources(sources_path)
        return [
            SourceInfo(id=s.id, type=s.type, enabled=s.enabled, tags=s.tags, detail=_describe(s))
            for s in cfg.sources if include_disabled or s.enabled
        ]

    @server.tool(annotations=read_only)
    def fetch_source(
        source_id: Annotated[str, Field(description="id from list_sources, e.g. tldr_ai")],
        since: Annotated[date | None, Field(description="fetch items received on or after this date (UTC); default: 2 days ago")] = None,
        max_items: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> FetchResult:
        """Fetch recent items from one source and return them normalised (title, url, summary, section, date).

        Read-only: it does not write the corpus or mark emails as read. Tracking links are already unwrapped and
        sponsored items dropped. Check `warnings`: scanned > 0 with matched == 0 means the match rule is wrong.
        """
        cfg = load_sources(sources_path)
        try:
            source = cfg.get(source_id)
        except KeyError:
            raise ToolError(f"unknown source {source_id!r}; known: {[s.id for s in cfg.sources]}") from None
        start = (datetime.combine(since, datetime.min.time(), tzinfo=UTC) if since
                 else datetime.now(UTC) - timedelta(days=2))
        try:
            report = build_adapter(source, cfg, mail_factory).fetch_report(start)
        except (FeedError, MailError, SourceNotSupported) as e:
            raise ToolError(str(e)) from e
        if not source.enabled:
            report.warnings.insert(0, f"source {source_id!r} is disabled; fetched anyway because you asked")
        return FetchResult(
            source_id=report.source_id, since=start, scanned=report.scanned, matched=report.matched,
            item_count=len(report.items), items=report.items[:max_items], skipped=report.skipped,
            deferred=report.deferred, warnings=report.warnings,
        )

    @server.tool(annotations=read_only)
    def suggest_newsletter_filter(
        account: Annotated[str, Field(description="mail account name from sources.yaml, e.g. work")],
        folder: Annotated[str, Field(description="mail folder, e.g. Inbox or Newsletters/TLDR")] = "Inbox",
        days: Annotated[int, Field(ge=1, le=60)] = 7,
    ) -> list[SenderGroup]:
        """Group recent emails in a folder by sender and propose a `match` block for each.

        Use this before subscribe_source for a newsletter: pick the group that is the newsletter and pass its
        proposed_match. Reads headers only (sender, subject, date), never message bodies.
        """
        cfg = load_sources(sources_path)
        if account not in cfg.mail_accounts:
            raise ToolError(f"unknown mail account {account!r}; known: {sorted(cfg.mail_accounts)}")
        try:
            reader = mail_factory(account, cfg)
            msgs = list(reader.list_messages(folder, datetime.now(UTC) - timedelta(days=days), with_body=False))
        except (MailError, SourceNotSupported) as e:
            raise ToolError(str(e)) from e
        return [SenderGroup(**g) for g in sender_groups(msgs)]

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    def subscribe_source(
        source_id: Annotated[str, Field(pattern=r"^[a-z0-9_]+$", description="new id, lower_snake_case")],
        type: Literal["email_newsletter", "rss"],
        tags: list[str] | None = None,
        url: Annotated[str | None, Field(description="rss only: feed URL")] = None,
        account: Annotated[str | None, Field(description="email_newsletter only: mail account name")] = None,
        folder: str = "Inbox",
        match: Annotated[dict[str, list[str]] | None, Field(description="email_newsletter only: e.g. a proposed_match from suggest_newsletter_filter")] = None,
        parser: Annotated[str, Field(description="email_newsletter only: tldr or generic")] = "tldr",
    ) -> SubscriptionChange:
        """Add a source to config/sources.yaml. Re-subscribing a disabled source re-enables it.

        For an rss feed the URL is fetched once to prove it parses. For a newsletter you must already have signed
        up on the publisher's site with the account's address; this only tells Signal Radar which emails are it.
        """
        cfg = load_sources(sources_path)
        existing = next((s for s in cfg.sources if s.id == source_id), None)
        if existing is not None:
            if existing.enabled:
                return SubscriptionChange(source_id=source_id, action="enabled", note="already subscribed, unchanged")
            existing.enabled = True
            save_sources(cfg, sources_path)
            return SubscriptionChange(source_id=source_id, action="enabled", note="re-enabled existing entry")
        try:
            if type == "rss":
                if not url:
                    raise ToolError("rss needs url")
                new = FeedSource(id=source_id, type="rss", url=url, tags=tags or [])
                note = _probe_feed(url)
            else:
                if not account or not match:
                    raise ToolError("email_newsletter needs account and match (see suggest_newsletter_filter)")
                new = EmailNewsletterSource.model_validate({
                    "id": source_id, "type": "email_newsletter", "account": account, "folder": folder,
                    "match": match, "parser": parser, "tags": tags or [],
                })
                note = "only emails received from now on, or within a fetch's look-back window, are picked up"
            cfg.sources.append(new)
            cfg = _revalidate(cfg)
        except ValueError as e:  # pydantic ValidationError is a ValueError
            raise ToolError(f"invalid source: {e}") from e
        save_sources(cfg, sources_path)
        return SubscriptionChange(source_id=source_id, action="subscribed", note=note)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
    def unsubscribe_source(source_id: str, purge: bool = False) -> SubscriptionChange:
        """Stop fetching a source. Default disables the entry; purge=True deletes it from sources.yaml.

        Corpus items already collected stay. Unsubscribing from the publisher's mailing list is manual.
        """
        cfg = load_sources(sources_path)
        try:
            source = cfg.get(source_id)
        except KeyError:
            raise ToolError(f"unknown source {source_id!r}") from None
        if purge:
            cfg.sources = [s for s in cfg.sources if s.id != source_id]
            action = "removed"
        else:
            source.enabled = False
            action = "disabled"
        save_sources(cfg, sources_path)
        return SubscriptionChange(source_id=source_id, action=action)

    _register_corpus_and_signals(server, mail_factory, sources_path, corpus_root, signals_db)
    return server


class IngestRun(BaseModel):
    source_id: str
    since: str
    fetched: int
    written: int = Field(description="new corpus files")
    duplicates: int
    merged: int = Field(description="existing items that gained another source (also_in)")
    skipped: list[str]
    deferred: int
    warnings: list[str]
    error: str | None


class IngestItemsResult(BaseModel):
    written: list[str]
    duplicates: int
    merged: list[str]


class SearchHit(BaseModel):
    id: str = Field(description="item id (use corpus://items/{id}) or entity node id")
    kind: Literal["item", "entity"]
    label: str = Field(description="item title or entity name")
    score: float = Field(description="cosine similarity to the query, 0..1")
    date: str | None = None
    url: str | None = None
    type: str | None = Field(None, description="entity type, e.g. COMPANY")
    source: str | None = Field(None, description="newsletter or feed id, for items")
    relevance: int | None = Field(None, description="0-3 relevance to the user's lens, for items")
    context: list[str] = Field(default_factory=list, description="one graph hop: mentioned entities / recent items")


class MoverOut(BaseModel):
    entity: str
    type: str
    current: int = Field(description="mentions in the window")
    baseline: float = Field(description="average mentions per window in the baseline periods")
    score: float
    families: int = Field(description="independent source families (e.g. TL;DR, AlphaSignal, Sifted) mentioning it in the window")
    first_seen: str
    new: bool
    confidence: str
    evidence: list[dict]


def _register_corpus_and_signals(server: MCPServer, mail_factory, sources_path, corpus_root, signals_db) -> None:
    """Phase 3/4 surface: ingest, corpus resources, extraction and signal queries."""
    from . import signals as sg
    from .corpus import Corpus
    from .extract import connect, extract_items
    from .ingest import IngestState, run_ingest

    def corpus() -> Corpus:
        return Corpus(corpus_root) if corpus_root else Corpus()

    def db():
        return connect(signals_db) if signals_db else connect()

    @server.tool(name="run_ingest",
                 annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    def run_ingest_tool(
        source_ids: Annotated[list[str] | None, Field(description="limit to these sources; default all enabled")] = None,
    ) -> list[IngestRun]:
        """Fetch sources since their last run and write new items to the corpus. Safe to repeat (dedupes)."""
        cfg = load_sources(sources_path)
        state = IngestState(signals_db.parent / "ingest_state.json") if signals_db else None
        runs = run_ingest(source_ids, cfg=cfg, corpus=corpus(), state=state, mail_factory=mail_factory)
        return [IngestRun(**{k: getattr(r, k) for k in IngestRun.model_fields}) for r in runs]

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def ingest_items(items: list[Item]) -> IngestItemsResult:
        """Write already-normalised items (e.g. from fetch_source) to the corpus. Dedupes by id, url and title."""
        res = corpus().write(items)
        return IngestItemsResult(written=res.written, duplicates=res.duplicates, merged=res.merged)

    @server.resource("corpus://items/{item_id}", mime_type="text/markdown",
                     description="One corpus item: YAML frontmatter (source, date, url, section, also_in) and summary.")
    def corpus_item(item_id: str) -> str:
        c = corpus()
        if item_id not in c:
            raise ValueError(f"no corpus item {item_id}")
        return c._by_id[item_id].read_text()

    @server.resource("corpus://digest/{day}", mime_type="text/markdown",
                     description="All corpus items of one day (YYYY-MM-DD), grouped by source and section.")
    def corpus_digest(day: str) -> str:
        d = date.fromisoformat(day)
        lines, last = [f"# Digest {day}"], None
        day_items = sorted(corpus().iter_meta(d, d), key=lambda mb: (mb[0]["source"], mb[0].get("section") or ""))
        for meta, body in day_items:
            head = f"{meta['source']} / {meta.get('section') or '-'}"
            if head != last:
                lines.append(f"\n## {head}")
                last = head
            lines.append(f"- [{meta['title']}]({meta['url']}) (id {meta['id']})")
        return "\n".join(lines) if len(lines) > 1 else f"# Digest {day}\n\nNo items."

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def extract_entities(item_id: str, redo: bool = False) -> dict:
        """Run entity extraction for one corpus item with the local `extraction` model and return the result.

        Stores the extraction; call it again with redo=true to force a new one. Run `signal-radar signals rebuild`
        (or the daily job) to fold it into the trend metrics.
        """
        c = corpus()
        if item_id not in c:
            raise ToolError(f"no corpus item {item_id}")
        con = db()
        try:
            stats = extract_items([item_id], corpus=c, db=con, redo=redo)
        except Exception as e:  # noqa: BLE001 - e.g. Ollama not running
            raise ToolError(f"extraction failed: {e}") from e
        row = con.execute("SELECT model, data FROM extractions WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            raise ToolError(f"extraction produced no result ({stats.failed} failed)")
        return {"item_id": item_id, "model": row[0], **json.loads(row[1])}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def top_movers(
        window_days: Annotated[int, Field(ge=3, le=90)] = 14,
        entity_type: Annotated[str | None, Field(description="Technology, Concept, Company, Product, Person, Regulation, Industry or Skill")] = None,
        direction: Literal["up", "down"] = "up",
        limit: Annotated[int, Field(ge=1, le=50)] = 15,
        min_relevance: Annotated[int, Field(ge=0, le=3, description="count only items at least this relevant to the user's lens")] = 0,
    ) -> list[MoverOut]:
        """Entities rising (or fading) most in the last window_days versus the previous four windows.

        Score is a rough z-score; `confidence` reflects evidence volume and how many independent sources agree.
        Each mover carries evidence item ids (read corpus://items/{id}).
        """
        return [MoverOut(**m.__dict__) for m in sg.top_movers(db(), window_days, entity_type, limit, direction=direction,
                                                               min_relevance=min_relevance)]

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def trend_metrics(entity: str, window_days: Annotated[int, Field(ge=3, le=90)] = 14) -> dict:
        """One entity over time: mentions per window (8 windows), velocity, first seen, related entities,
        stated relations and evidence. Accepts aliases (e.g. "MCP")."""
        out = sg.trend_metrics(entity, db(), window_days)
        if out is None:
            raise ToolError(f"no entity matching {entity!r} in the signal store")
        return out

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def emerging_clusters(window_days: Annotated[int, Field(ge=7, le=90)] = 14, limit: int = 10) -> list[dict]:
        """Entity pairs mentioned together more often now than in the previous windows: clusters forming."""
        return sg.cooccurrence_growth(db(), window_days, limit=limit)

    @server.resource("brief://latest", mime_type="text/markdown",
                     description="The latest weekly signal brief (computed signals, evidence links, model-written prose).")
    def brief_latest() -> str:
        from .brief import BRIEFS_DIR

        files = sorted(BRIEFS_DIR.glob("*.md"))
        if not files:
            raise ValueError("no weekly brief yet; run `signal-radar brief`")
        return files[-1].read_text()

    STYLE_RULES = ("Write without em dashes or en dashes, without 'delve', 'leverage', symmetric sentence pairs or "
                   "filler adjectives. Original wording only.")

    @server.prompt(title="Weekly signal brief", description="Walk through this week's signals with evidence, through the user's lens.")
    def weekly_signal_brief(days: str = "7") -> str:
        return f"""Give me my weekly signal brief for the last {days} days.

1. Read the resource brief://latest if it covers this period; otherwise build the picture from the tools:
   top_movers (window_days={days}, direction up, then down, min_relevance 1), emerging_clusters, reading_list.
2. For each signal state: what moved (counts vs baseline), confidence, and the evidence item ids. Read
   corpus://items/{{id}} for any item you rely on.
3. Add one "so what for me" line per signal, through my lens: financial services, risk, compliance, EU AI Act,
   enterprise architecture, AI governance. If a signal barely matters to me, say so.
4. Never present a signal without evidence. Say plainly where evidence is thin.
{STYLE_RULES}"""

    @server.prompt(title="Learning radar", description="Skills and technologies worth learning now, each with a concrete project.")
    def learning_radar(weeks: str = "4") -> str:
        return f"""Build my learning radar from the last {weeks} weeks.

1. Use top_movers (window_days={int(weeks) * 7 if weeks.isdigit() else 28}, entity_type Technology, then Concept,
   min_relevance 1) and trend_metrics for the strongest candidates.
2. Rank by momentum and fit with my current skills: enterprise and data architecture, AI governance, risk and
   compliance in financial services.
3. For each of the top 3: why now (with evidence item ids), how it connects to what I already know, and one
   concrete learning project I can finish in a few days.
Say so if the evidence does not support three items. {STYLE_RULES}"""

    @server.prompt(title="Content ideas", description="LinkedIn posts and thought-leadership outlines grounded in cited signals.")
    def content_ideas(topic: str = "", count: str = "3") -> str:
        focus = f" about {topic}" if topic else ""
        return f"""Suggest {count} LinkedIn post ideas{focus} for me, grounded in current signals.

1. Find material with reading_list (days 14), top_movers (window_days 14) and, if a topic is given, trend_metrics.
2. For each idea: a working title, the angle through my lens (financial services, risk, compliance, EU AI Act,
   enterprise architecture), 3 to 5 bullet points, and the evidence item ids it rests on.
3. Prefer angles where I can add expert judgement over restating news.
{STYLE_RULES} Do not invent facts or numbers; everything must trace to an item."""

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def reading_list(
        days: Annotated[int, Field(ge=1, le=60)] = 7,
        category: Annotated[str | None, Field(description="a category id from config/taxonomy.yaml")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> list[dict]:
        """Items most worth reading for the user's lens, with the reason and which sources covered each."""
        return sg.reading_list(db(), days, limit, category=category)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def semantic_search(
        query: Annotated[str, Field(min_length=3, description="a question or topic in plain words")],
        k: Annotated[int, Field(ge=1, le=30)] = 10,
        kind: Annotated[Literal["all", "item", "entity"], Field(description="news items, entities, or both")] = "all",
    ) -> list[SearchHit]:
        """Find news items and entities by meaning, not exact words (vector search, ADR-0019).

        Use it for conceptual questions ("how are banks handling AI model risk", "EU enforcement") where keywords
        miss paraphrases. Each hit has a cosine score (higher is closer; above about 0.8 is usually on topic) and
        `context` from one graph hop: the entities an item mentions, or recent items that mention an entity.
        For named-entity trends use trend_metrics; read an item in full with the corpus://items/{id} resource.
        """
        from . import vectors

        want = None if kind == "all" else kind
        try:
            from .config import load_graph
            from .graph_sync import neo4j_target

            t = neo4j_target(load_graph())
            try:
                hits = vectors.search(query, k=k, kind=want, target=t)
            finally:
                t.close()
        except Exception:  # noqa: BLE001 - Neo4j down: exact search over the local vector cache
            hits = vectors.search_local(query, k=k, kind=want)
        if not hits:
            raise ToolError("no vectors yet: run `signal-radar graph embed` in a terminal")
        return [SearchHit(**vectors.hit_dict(h)) for h in hits]


def _revalidate(cfg):
    """Re-validate the whole config (duplicate ids, account references) after an in-place change."""
    from .config import SourcesConfig

    return SourcesConfig.model_validate(cfg.model_dump(by_alias=True))


def _probe_feed(url: str) -> str:
    import feedparser
    import httpx

    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "signal-radar/0.2"})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ToolError(f"could not fetch feed: {e}") from e
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ToolError(f"not a parseable RSS/Atom feed: {feed.bozo_exception}")
    return f"feed OK: {feed.feed.get('title', '?')!r}, {len(feed.entries)} entries"


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    load_dotenv(PROJECT_ROOT / ".env")
    build_server().run("stdio")


if __name__ == "__main__":
    main()
