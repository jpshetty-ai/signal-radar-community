from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from signal_radar.config import DATA_DIR
from signal_radar.sources.parsers import SkipMessage, parse_bayarealetters

DOC = (Path(__file__).parent / "fixtures" / "bayarealetters_synthetic.html").read_text()
RECEIVED = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every link is a per-subscriber redirect (ADR-0016): parsing must never touch the network."""
    def refuse(*a, **k):
        raise AssertionError("network access during Bay Area Letters parsing")
    monkeypatch.setattr(httpx, "get", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


def parse(html=DOC):
    return parse_bayarealetters(html, source_id="bayarealetters", received_at=RECEIVED,
                                origin_ref="<msg-1@mail.wirely.so>", tags=["tech"])


def test_stories_are_h3_plus_tldr_and_nothing_else():
    items = parse()
    assert [i.title for i in items] == ["Example Lab Raises $1B", "Regulator Opens AI Hearing"]  # no promo, sponsor, preview
    assert items[0].summary == "Example Lab raised $1 billion to build agents for banks. The round values it at $10 billion."
    assert all(i.date == date(2026, 10, 8) and i.edition == "Bay Area Letters" and i.tags == ["tech"] for i in items)


def test_links_point_to_the_issue_in_gmail_never_to_a_tracked_redirect():
    items = parse()
    assert {i.url for i in items} == {"https://mail.google.com/mail/#search/rfc822msgid%3Amsg-1%40mail.wirely.so"}
    assert not any("bayarealetters.com" in i.url or "SUBSCRIBER" in i.url for i in items)
    assert len({i.id for i in items}) == len(items)  # one URL per issue; titles tell stories apart


def test_issue_without_stories_is_skipped():
    with pytest.raises(SkipMessage):
        parse("<html><h3>Welcome to Bay Area Letters</h3><p>Thanks for subscribing.</p></html>")


SAMPLES = sorted((DATA_DIR / "samples" / "bayarealetters").glob("*.html"))  # real issues, git-ignored


@pytest.mark.skipif(not SAMPLES, reason="no real Bay Area Letters samples")
@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_real_samples(path):
    items = parse(path.read_text())
    assert 10 <= len(items) <= 18
    assert all(i.summary and not i.summary.startswith("TL;DR") and not i.summary.endswith("→") for i in items)
    assert not any(t in {"Keep reading", "Let the Future Come to Your Inbox"} for t in (i.title for i in items))
