from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from signal_radar.config import DATA_DIR
from signal_radar.items import canonical_url, item_id
from signal_radar.sources.parsers.tldr import parse_tldr, unwrap_tracking

FIXTURE = Path(__file__).parent / "fixtures" / "tldr_ai_synthetic.html"
RECEIVED = datetime(2026, 10, 2, 13, 57, tzinfo=UTC)


def parse(html: str):
    return parse_tldr(html, source_id="tldr_ai", received_at=RECEIVED, origin_ref="<m1>", tags=["ai"])


def test_unwrap_tracking_link():
    wrapped = "https://tracking.tldrnewsletter.com/CL0/https:%2F%2Fa.com%2Fp%3Fx=1%26utm_source=t/1/msg/sig=4"
    assert unwrap_tracking(wrapped) == "https://a.com/p?x=1&utm_source=t"
    assert unwrap_tracking("https://plain.com/") == "https://plain.com/"


def test_canonical_url_strips_tracking_and_is_stable():
    assert canonical_url("HTTPS://News.Example.com/a/?utm_source=x&id=7#frag") == "https://news.example.com/a?id=7"
    assert item_id("https://a.com/x?utm_medium=y", "Title  Here") == item_id("https://a.com/x/", "title here")


def test_parses_items_sections_and_metadata():
    items = parse(FIXTURE.read_text())
    titles = [i.title for i in items]
    assert titles == [
        "Example Lab ships a new reasoning model",
        "EU regulator fines a chatbot provider",
        "agent-kit",
        "Why routine work matters",
    ]
    first = items[0]
    assert first.url == "https://www.example-lab.ai/research/new-model"
    assert first.section == "Headlines & Launches"
    assert first.edition == "TLDR AI" and first.date == date(2026, 10, 2)
    assert first.read_minutes == 4 and first.content_kind is None
    assert first.summary.startswith("Example Lab released a model") and "available today" in first.summary
    assert first.tags == ["ai"] and first.origin_ref == "<m1>"
    assert items[1].url == "https://news.example.com/2026/10/01/regulator-fines?id=7"  # span-wrapped block
    assert items[1].summary == "The fine is the first under the AI Act's transparency rules."
    assert items[2].content_kind == "GitHub Repo" and items[2].section == "Engineering & Research"


def test_drops_sponsors_own_links_and_duplicates():
    items = parse(FIXTURE.read_text())
    assert not any("Sponsor" in i.title or "TLDR" in i.title for i in items)
    assert not any("tracking.tldrnewsletter.com" in i.url or "utm_" in i.url for i in items)
    assert len({i.id for i in items}) == len(items) == 4


def test_tech_edition_name_and_fallback_date():
    html = FIXTURE.read_text().replace("TLDR AI <span id=\"date\">2026-10-02", "TLDR <span id=\"date\">2026-10-01")
    assert {i.edition for i in parse(html)} == {"TLDR"}
    assert parse(html)[0].date == date(2026, 10, 1)
    no_heading = FIXTURE.read_text().replace("TLDR AI <span id=\"date\">2026-10-02", "Something <span>else")
    assert parse(no_heading)[0].date == RECEIVED.date()


# Real issues saved with `signal-radar mail-senders work Newsletters/TLDR --save-html data/samples/tldr`.
# They stay in data/ (git-ignored: they carry personal unsubscribe tokens); the test runs when they exist.
SAMPLES = sorted((DATA_DIR / "samples" / "tldr").glob("*.html"))


@pytest.mark.skipif(not SAMPLES, reason="no real TL;DR samples in data/samples/tldr")
@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_real_samples_parse_cleanly(path):
    html = path.read_text()
    if "Confirm your signup" in html or "Confirm Signup" in html:
        pytest.skip("signup confirmation, not an issue")
    items = parse(html)
    assert len(items) >= 5, "a real issue has many items; fewer means the template changed"
    for i in items:
        assert "tracking.tldrnewsletter.com" not in i.url and "utm_" not in i.url
        assert "(Sponsor)" not in i.title and i.title and i.summary
        assert i.edition and i.edition.startswith("TLDR")


def test_canonical_url_survives_double_escaped_ampersands():
    assert canonical_url("https://c.com/x.html?taid=6a&amp%3Butm_source=tldrai") == "https://c.com/x.html?taid=6a"
    assert canonical_url("https://a.com/p?x=1&amp;amp;utm_source=t&id=7") == "https://a.com/p?x=1&id=7"
