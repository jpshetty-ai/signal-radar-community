import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from signal_radar.config import DATA_DIR
from signal_radar.sources.parsers import SkipMessage, parse_alphasignal

DOC = (Path(__file__).parent / "fixtures" / "alphasignal_synthetic.html").read_text()
RECEIVED = datetime(2026, 10, 2, 14, 29, tzinfo=UTC)


def as_email(doc: str) -> str:
    """The email rendering: every http link replaced by an opaque per-subscriber redirect."""
    n = iter(range(1000))
    return re.sub(r'href="https?://[^"]+"', lambda m: f'href="https://app.alphasignal.ai/c?uid=ME&lid={next(n)}"', doc)


ISSUE_PAGE = "https://alphasignal.ai/email/0123456789abcdef"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Terms §6 (ADR-0015): parsing must never touch the network."""
    def refuse(*a, **k):
        raise AssertionError("network access during AlphaSignal parsing")
    monkeypatch.setattr(httpx, "get", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


def parse(email_html=None, doc=DOC):
    return parse_alphasignal(email_html if email_html is not None else as_email(doc), source_id="alphasignal",
                             received_at=RECEIVED, origin_ref="<m>", tags=["ai"])


def test_items_come_from_the_email_and_link_to_the_issue_page():
    items = parse()
    assert [i.title for i in items] == [
        "Example Lab opens its agent to plugins",
        "Open-source tool scores text for AI-ness",
        "New diffusion model beats a larger one",
        "Tiny model fits on a 16 GB GPU",
    ]
    assert {i.url for i in items} == {ISSUE_PAGE}            # built from the id, no tracker, no uid
    assert len({i.id for i in items}) == len(items)           # one URL, the title tells the stories apart


def test_sections_engagement_and_summaries():
    items = parse()
    story, repo, sig, model = items
    assert (story.section, story.engagement) == ("Top News", {"likes": 16862})
    assert story.summary == "Example Lab added a plugin system to its coding agent. Plugins run with full machine access."
    assert (repo.section, repo.engagement) == ("Top Repo", {"stars": 1200})
    assert (sig.section, sig.title, sig.engagement) == ("Signals", "New diffusion model beats a larger one", {"likes": 444})
    assert model.engagement == {"downloads": 3388} and model.summary == ""
    assert all(i.edition == "AlphaSignal" and i.date == date(2026, 10, 2) and i.tags == ["ai"] for i in items)


def test_drops_banner_feature_and_signal_sponsors_and_footer():
    items = parse()
    titles = " ".join(i.title for i in items)
    for paid in ("Upcoming Workshop", "PaidCo", "PaidLab", "WORK WITH US", "Awesome"):
        assert paid not in titles


def test_sunday_deep_dive_is_skipped():
    deep = "<table><tr><td>Sunday Deep Dive</td></tr><tr><td class='h1'>How to pick an agent harness</td></tr></table>"
    with pytest.raises(SkipMessage):
        parse(email_html=deep)


def test_skips_an_issue_without_a_public_page():
    with pytest.raises(SkipMessage, match="no web-version link"):
        parse(email_html=as_email(DOC).replace("0123456789abcdef", "nope"))


# Real issues live in data/samples/alphasignal (git-ignored: personal tokens).
SAMPLES = sorted((DATA_DIR / "samples" / "alphasignal").glob("*.html"))


@pytest.mark.skipif(not SAMPLES, reason="no real AlphaSignal samples")
@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_real_samples(path):
    try:
        items = parse_alphasignal(path.read_text(), source_id="a", received_at=RECEIVED)
    except SkipMessage:
        return
    assert 5 <= len(items) <= 12
    for i in items:
        assert re.fullmatch(r"https://alphasignal\.ai/email/[0-9a-f]{16}", i.url) and i.engagement
        assert "buysellads" not in i.title.lower()
