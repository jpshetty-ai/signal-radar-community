from datetime import UTC, datetime

import pytest

from signal_radar.config import FeedSource, SourcesConfig
from signal_radar.sources import FeedAdapter, FeedError, build_adapter
from signal_radar.sources.feed import clean_summary, page_description

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Example EU</title>
<item><title>Fintech raises &#8364;40M</title><link>https://example.eu/a/?utm_source=rss</link>
  <guid>a</guid><pubDate>Fri, 02 Oct 2026 12:00:00 +0000</pubDate><category>General</category><category>Fintech</category>
  <description>Cut short by the CMS and glued.Together...</description>
  <content:encoded><![CDATA[<p>The <em>lead</em> paragraph, in full.</p><p>Second paragraph.</p>]]></content:encoded></item>
<item><title>Title only</title><link>https://example.eu/b</link><guid>b</guid>
  <pubDate>Thu, 01 Oct 2026 08:00:00 +0000</pubDate></item>
<item><title>Old news</title><link>https://example.eu/c</link><guid>c</guid>
  <pubDate>Mon, 21 Sep 2026 08:00:00 +0000</pubDate><description>Too old.</description></item>
<item><title>Fintech raises &#8364;40M</title><link>https://example.eu/a/</link><guid>a-again</guid>
  <pubDate>Fri, 02 Oct 2026 12:00:00 +0000</pubDate><description>Duplicate entry.</description></item>
</channel></rss>"""

HEAD = '<html><head><meta property="og:description" content="From the page head."></head><body>'


def adapter(heads=None, **kw):
    src = FeedSource(id="example_eu", type="rss", url="https://example.eu/feed", tags=["eu"], **kw)
    calls = []

    def head(url):
        calls.append(url)
        return (heads or {}).get(url)

    return FeedAdapter(src, fetch_feed=lambda url: RSS.encode(), fetch_head=head), calls


def test_items_since_cursor_deduped_with_canonical_url_and_section():
    a, _ = adapter()
    report = a.fetch_report(datetime(2026, 9, 30, tzinfo=UTC))
    assert report.scanned == 4 and report.matched == 3
    assert [i.title for i in report.items] == ["Fintech raises €40M", "Title only"]
    first = report.items[0]
    assert first.url == "https://example.eu/a" and first.edition == "Example EU" and first.section == "Fintech"
    assert first.tags == ["eu"] and str(first.date) == "2026-10-02" and first.origin_ref == "a"


def test_truncated_cms_excerpt_falls_back_to_content_lead_with_clean_spacing():
    a, _ = adapter()
    first = a.fetch_report(datetime(2026, 9, 30, tzinfo=UTC)).items[0]
    assert first.summary == "The lead paragraph, in full. Second paragraph."


def test_title_only_entry_reads_page_meta_only_when_enabled():
    a, calls = adapter()
    assert a.fetch_report(datetime(2026, 9, 30, tzinfo=UTC)).items[1].summary == ""
    assert calls == []
    a, calls = adapter(heads={"https://example.eu/b": HEAD}, page_meta=True, edition="Ex")
    item = a.fetch_report(datetime(2026, 9, 30, tzinfo=UTC)).items[1]
    assert item.summary == "From the page head." and item.edition == "Ex"
    assert calls == ["https://example.eu/b"]  # entries that already have a summary are not fetched


def test_warns_when_feed_does_not_reach_back_to_since():
    a, _ = adapter()
    assert any("only reaches back to 2026-09-21" in w for w in a.fetch_report(datetime(2026, 9, 1, tzinfo=UTC)).warnings)
    assert not a.fetch_report(datetime(2026, 9, 30, tzinfo=UTC)).warnings


def test_clean_summary_strips_patterns_repeated_title_and_cuts_at_a_word():
    raw = "<p>Big Title</p><p>Anonymous (not verified) Tue, 09/29/2026 - 10:15 The real text goes on and on.</p>"
    strip = [r"Anonymous \(not verified\)", r"\b\w{3}, \d{2}/\d{2}/\d{4} - \d{2}:\d{2}\b"]
    assert clean_summary(raw, "Big Title", strip, 700) == "The real text goes on and on."
    assert clean_summary("one two three four", "", [], 100) == "one two three four"
    assert clean_summary("word " * 50, "", [], 100).endswith(" …")
    assert page_description('<meta name="description" content=" x ">') == "x"


def test_not_a_feed_raises_feed_error():
    src = FeedSource(id="bad", type="rss", url="https://example.eu/feed")
    with pytest.raises(FeedError):
        FeedAdapter(src, fetch_feed=lambda url: b"<html>not a feed").fetch_report(datetime(2026, 1, 1, tzinfo=UTC))


def test_build_adapter_returns_feed_adapter_and_bad_strip_regex_is_rejected():
    cfg = SourcesConfig.model_validate({"sources": [{"id": "f", "type": "rss", "url": "https://example.eu/feed"}]})
    assert isinstance(build_adapter(cfg.sources[0], cfg), FeedAdapter)
    with pytest.raises(ValueError):
        FeedSource(id="f", type="rss", url="https://example.eu/feed", strip=["(unclosed"])
