from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from signal_radar.config import SourcesConfig
from signal_radar.corpus import Corpus, read_file
from signal_radar.ingest import IngestState, backfill_archive, run_ingest
from signal_radar.items import Item, item_id
from signal_radar.sources import MailMessage
from signal_radar.sources.parsers.tldr_web import parse_tldr_web

FIX = Path(__file__).parent / "fixtures"
TLDR_HTML = (FIX / "tldr_ai_synthetic.html").read_text()


def item(url="https://a.com/x", title="A story", source="tldr_ai", day=date(2026, 10, 1), **kw):
    return Item(id=item_id(url, title), source_id=source, date=day, url=url, title=title, summary="Sum.", **kw)


def test_write_is_idempotent_and_round_trips(tmp_path):
    c = Corpus(tmp_path)
    res = c.write([item(section="Headlines", engagement={"likes": 5})])
    assert len(res.written) == 1
    assert c.write([item()]).duplicates == 1
    meta, body = read_file(next(tmp_path.glob("2026/10/*.md")))
    assert meta["section"] == "Headlines" and meta["engagement"] == {"likes": 5} and "Sum." in body
    assert len(Corpus(tmp_path)) == 1  # a fresh instance indexes from disk


def test_same_url_from_another_source_becomes_also_in(tmp_path):
    c = Corpus(tmp_path)
    c.write([item()])
    res = c.write([item(title="Different headline", source="alphasignal", engagement={"likes": 900})])
    assert res.merged and not res.written
    meta, _ = read_file(next(tmp_path.glob("2026/10/*.md")))
    assert meta["also_in"] == [{"source": "alphasignal", "date": "2026-10-01", "title": "Different headline",
                                "engagement": {"likes": 900}}]
    assert c.write([item(title="Again", source="alphasignal")]).duplicates == 1  # recorded once per source


def test_stories_sharing_an_alphasignal_issue_url_stay_separate(tmp_path):
    """Since ADR-0015 every AlphaSignal item links to its issue page: the URL alone must not merge them."""
    c = Corpus(tmp_path)
    page = "https://alphasignal.ai/email/0123456789abcdef"
    res = c.write([item(url=page, title="Story one", source="alphasignal"),
                   item(url=page, title="Story two", source="alphasignal")])
    assert len(res.written) == 2 and not res.merged
    assert len(c.write([item(url=page, title="Story three", source="tldr_ai")]).written) == 1


def test_short_link_copy_of_same_story_is_a_duplicate(tmp_path):
    c = Corpus(tmp_path)
    c.write([item(url="https://openai.com/index/x", title="Why routine work matters")])
    res = c.write([item(url="https://links.tldrnewsletter.com/abc", title="Why routine work matters")])
    assert res.duplicates == 1 and not res.written


class FakeMail:
    def __init__(self, msgs):
        self.msgs, self.since = msgs, []

    def list_messages(self, folder, since, with_body=True, senders=None):
        self.since.append(since)
        return iter(self.msgs)


def cfg():
    return SourcesConfig.model_validate({
        "mail_accounts": {"m": {"kind": "imap", "host": "h", "user_env": "U", "keyring_service": "k"}},
        "sources": [{"id": "tldr_ai", "type": "email_newsletter", "account": "m", "folder": "N",
                     "match": {"from": ["tldrnewsletter.com"], "from_name": ["TLDR AI"]}, "parser": "tldr",
                     "archive": {"url": "https://tldr.tech/ai/{date}", "parser": "tldr_web"}}],
    })


def msg():
    return MailMessage("1", "<1>", "s", "dan@tldrnewsletter.com", "TLDR AI", datetime(2026, 10, 2, 12, tzinfo=UTC), TLDR_HTML)


def test_ingest_writes_corpus_and_moves_cursor_with_overlap(tmp_path):
    mail, state = FakeMail([msg()]), IngestState(tmp_path / "state.json")
    now = datetime(2026, 10, 3, 6, tzinfo=UTC)
    runs = run_ingest(cfg=cfg(), corpus=Corpus(tmp_path / "c"), state=state, mail_factory=lambda a, c: mail, now=now)
    assert runs[0].written == 4 and runs[0].error is None
    assert mail.since[0] == datetime(2026, 9, 26, 6, tzinfo=UTC)  # first run: 7 days back
    assert IngestState(tmp_path / "state.json").cursor("tldr_ai") == now
    runs = run_ingest(cfg=cfg(), corpus=Corpus(tmp_path / "c"), state=IngestState(tmp_path / "state.json"),
                      mail_factory=lambda a, c: mail, now=datetime(2026, 10, 4, 6, tzinfo=UTC))
    assert mail.since[1] == datetime(2026, 10, 1, 6, tzinfo=UTC)  # cursor minus 2 days overlap
    assert runs[0].written == 0 and runs[0].duplicates == 4


def test_ingest_isolates_a_failing_source(tmp_path):
    def broken(a, c):
        raise RuntimeError("mailbox down")
    runs = run_ingest(cfg=cfg(), corpus=Corpus(tmp_path), state=IngestState(tmp_path / "s.json"), mail_factory=broken)
    assert "mailbox down" in runs[0].error


WEB_PAGE = """<html><body><h3>TLDR AI 2026-10-01</h3>
<article><a href="https://sponsor.example/x?utm_source=tldr"><h3>Buy this (Sponsor)</h3></a><div class="newsletter-html">Ad.</div></article>
<h3>Headlines &amp; Launches</h3>
<article><a href="https://www.example-lab.ai/research/new-model?utm_source=tldrai"><h3>Example Lab ships a new reasoning model (4 minute read)</h3></a>
<div class="newsletter-html">Example Lab released a model.</div></article>
<h3>Quick Links</h3>
<article><a href="https://github.com/example/kit?utm_source=tldrai"><h3>kit (GitHub Repo)</h3></a><div class="newsletter-html">Toolkit.</div></article>
<article><a href="https://jobs.ashbyhq.com/tldr.tech/1"><h3>Work at TLDR</h3></a><div class="newsletter-html">Job.</div></article>
</body></html>"""


def test_web_archive_parser_matches_email_ids():
    items = parse_tldr_web(WEB_PAGE, source_id="tldr_ai", issue_date=date(2026, 10, 1), edition="TLDR AI")
    assert [(i.title, i.section, i.read_minutes, i.content_kind) for i in items] == [
        ("Example Lab ships a new reasoning model", "Headlines & Launches", 4, None),
        ("kit", "Quick Links", None, "GitHub Repo"),
    ]
    assert items[0].id == item_id("https://www.example-lab.ai/research/new-model", "Example Lab ships a new reasoning model")


def test_backfill_is_polite_cached_and_skips_missing_days(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return None if url.endswith("2026-09-30") else WEB_PAGE

    kw = dict(cfg=cfg(), corpus=Corpus(tmp_path / "c"), fetch=fetch, delay_s=0, cache_dir=tmp_path / "cache",
              today=date(2026, 10, 2))
    run = backfill_archive("tldr_ai", 3, **kw)  # 29 Sep (Mon) .. 2 Oct (Thu): 4 weekdays, one 404
    assert len(calls) == 4 and run.scanned == 3 and run.written == 2  # same page each day -> same ids after day 1
    backfill_archive("tldr_ai", 3, **kw)
    assert len(calls) == 4  # second run: everything served from cache, 404 remembered


def test_backfill_requires_an_archive(tmp_path):
    c = cfg()
    c.sources[0].archive = None
    with pytest.raises(ValueError, match="no web archive"):
        backfill_archive("tldr_ai", 3, cfg=c, corpus=Corpus(tmp_path), delay_s=0)


def test_deferred_issue_holds_cursor_but_not_forever(tmp_path):
    from signal_radar.sources.base import FetchReport

    class Adapter:
        def __init__(self, deferred_at):
            self.deferred_at = deferred_at

        def fetch_report(self, since):
            return FetchReport("tldr_ai", since, deferred=1, deferred_dates=[self.deferred_at])

    import signal_radar.ingest as ing
    now = datetime(2026, 10, 3, tzinfo=UTC)
    for deferred_at, expected in ((datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
                                  (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC))):
        state = IngestState(tmp_path / "s.json")
        orig = ing.build_adapter
        ing.build_adapter = lambda s, c, f, d=deferred_at: Adapter(d)
        try:
            run_ingest(cfg=cfg(), corpus=Corpus(tmp_path / "c"), state=state, now=now)
        finally:
            ing.build_adapter = orig
        assert state.cursor("tldr_ai") == expected
