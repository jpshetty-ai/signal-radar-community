"""The corpus: one Markdown file per news item, YAML frontmatter + summary, Git-tracked.

The corpus is the durable record. The signal store, the Graqle graph and any website are rebuilt from it, so
the writer is careful about one thing above all: the same story must not become two files.

Dedupe, in order:
1. same item id (canonical url + title)                       -> already stored
2. same canonical url from another source or edition          -> recorded in the existing file's `also_in`
3. same source, same date, same normalised title (e.g. the email and the web-archive copy of one issue,
   where one has a short link and the other the real URL)       -> already stored

`also_in` is worth keeping: a story picked up by two independent curators is stronger evidence than one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from .config import PROJECT_ROOT
from .items import Item

CORPUS_DIR = PROJECT_ROOT / "corpus"
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)
# URLs shared by many stories, so they identify no single story: TL;DR short links, the AlphaSignal issue page
# (ADR-0015) and the Gmail link of a Bay Area Letters issue (ADR-0016). Never used for the cross-source URL merge.
_SHARED_URL_MARKERS = ("links.tldrnewsletter.com", "alphasignal.ai/email/", "mail.google.com/mail/")


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _is_shared_url(url: str) -> bool:
    return any(m in url for m in _SHARED_URL_MARKERS)


@dataclass
class WriteResult:
    written: list[str] = field(default_factory=list)      # new item ids
    duplicates: int = 0                                    # already stored, nothing changed
    merged: list[str] = field(default_factory=list)        # existing ids that gained an also_in entry


def item_to_markdown(item: Item, also_in: list[dict] | None = None) -> str:
    meta = {
        "id": item.id,
        "date": item.date.isoformat(),
        "source": item.source_id,
        "edition": item.edition,
        "url": item.url,
        "title": item.title,
        "section": item.section,
        "tags": item.tags,
        "read_minutes": item.read_minutes,
        "content_kind": item.content_kind,
        "engagement": item.engagement or None,
        "origin_ref": item.origin_ref,
        "ingested_at": (item.fetched_at or datetime.now(UTC)).replace(microsecond=0).isoformat(),
        "also_in": also_in or None,
    }
    meta = {k: v for k, v in meta.items() if v not in (None, [], {})}
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
    body = f"# {item.title}\n\n{item.summary}\n\n[Source]({item.url})\n" if item.summary else f"# {item.title}\n\n[Source]({item.url})\n"
    return f"---\n{front}\n---\n\n{body}"


def read_file(path: Path) -> tuple[dict, str]:
    m = _FRONTMATTER.match(path.read_text())
    if not m:
        raise ValueError(f"{path} has no frontmatter")
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


def _summary_from_body(body: str) -> str:
    lines = [ln for ln in body.strip().splitlines() if ln and not ln.startswith("# ") and not ln.startswith("[Source](")]
    return "\n".join(lines).strip()


def meta_to_item(meta: dict, body: str = "") -> Item:
    d = meta["date"]
    return Item(
        id=meta["id"], source_id=meta["source"], date=d if isinstance(d, date) else date.fromisoformat(str(d)),
        url=meta["url"], title=meta["title"], summary=_summary_from_body(body), section=meta.get("section"),
        tags=meta.get("tags") or [], read_minutes=meta.get("read_minutes"), content_kind=meta.get("content_kind"),
        edition=meta.get("edition"), engagement=meta.get("engagement") or {}, origin_ref=meta.get("origin_ref"),
    )


class Corpus:
    def __init__(self, root: Path | None = None):
        self.root = root or CORPUS_DIR
        self._by_id: dict[str, Path] = {}
        self._by_url: dict[str, str] = {}
        self._by_day_title: dict[tuple[str, str, str], str] = {}
        self._loaded = False

    # ------------------------------------------------------------ index

    def _path_for(self, item: Item) -> Path:
        return self.root / f"{item.date:%Y}" / f"{item.date:%m}" / f"{item.date.isoformat()}-{item.id}.md"

    def _index(self, meta: dict, path: Path) -> None:
        iid = meta["id"]
        self._by_id[iid] = path
        if not _is_shared_url(meta["url"]):
            self._by_url.setdefault(meta["url"], iid)
        self._by_day_title[(meta["source"], str(meta["date"]), _norm_title(meta["title"]))] = iid

    def load(self) -> Corpus:
        if not self._loaded:
            for path in sorted(self.root.glob("*/*/*.md")):
                meta, _ = read_file(path)
                self._index(meta, path)
            self._loaded = True
        return self

    def __len__(self) -> int:
        return len(self.load()._by_id)

    def __contains__(self, item_id: str) -> bool:
        return item_id in self.load()._by_id

    # ------------------------------------------------------------ write

    def write(self, items: Iterable[Item]) -> WriteResult:
        self.load()
        result = WriteResult()
        for item in items:
            if item.id in self._by_id:
                result.duplicates += 1
                continue
            same_day = self._by_day_title.get((item.source_id, item.date.isoformat(), _norm_title(item.title)))
            if same_day:
                result.duplicates += 1
                continue
            existing = None if _is_shared_url(item.url) else self._by_url.get(item.url)
            if existing:
                if self._add_also_in(existing, item):
                    result.merged.append(existing)
                else:
                    result.duplicates += 1
                continue
            path = self._path_for(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(item_to_markdown(item))
            tmp.replace(path)
            self._index({"id": item.id, "url": item.url, "source": item.source_id,
                         "date": item.date.isoformat(), "title": item.title}, path)
            result.written.append(item.id)
        return result

    def _add_also_in(self, existing_id: str, item: Item) -> bool:
        path = self._by_id[existing_id]
        meta, body = read_file(path)
        also = meta.get("also_in") or []
        entry = {"source": item.source_id, "date": item.date.isoformat(), "title": item.title}
        if item.engagement:
            entry["engagement"] = item.engagement
        if meta["source"] == item.source_id or any(a["source"] == item.source_id for a in also):
            return False
        also.append(entry)
        meta["also_in"] = also
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(f"---\n{front}\n---\n{body if body.startswith(chr(10)) else chr(10) + body}")
        tmp.replace(path)
        return True

    # ------------------------------------------------------------- read

    def get(self, item_id: str) -> tuple[dict, str]:
        path = self.load()._by_id.get(item_id)
        if path is None:
            raise KeyError(item_id)
        return read_file(path)

    def iter_meta(self, start: date | None = None, end: date | None = None) -> Iterator[tuple[dict, str]]:
        """(frontmatter, body) for every item, oldest first, optionally limited to [start, end]."""
        for path in sorted(self.load()._by_id.values()):
            day = path.name[:10]
            if (start and day < start.isoformat()) or (end and day > end.isoformat()):
                continue
            yield read_file(path)
