"""Drive the MCP server in-process through the real protocol (mcp.Client), with a fake mailbox."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
import yaml
from mcp import Client

from signal_radar.config import CONFIG_DIR
from signal_radar.mcp_server import build_server
from signal_radar.sources import MailAuthRequired, MailMessage

FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "tldr_ai_synthetic.html").read_text()


class FakeMail:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail

    def list_messages(self, folder, since, with_body=True, senders=None):
        if self.fail:
            raise self.fail
        for i, name in enumerate(["TLDR AI", "TLDR", "TLDR AI"]):
            yield MailMessage(id=f"m{i}", internet_message_id=f"<m{i}>", subject=f"Issue {i}",
                              from_address="dan@tldrnewsletter.com", from_name=name,
                              received_at=datetime.now(UTC), html=FIXTURE_HTML if with_body else None)


@pytest.fixture
def sources_path(tmp_path):
    p = tmp_path / "sources.yaml"
    shutil.copy(CONFIG_DIR / "sources.yaml", p)
    return p


def call(server, tool, args=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args or {})
    return anyio.run(go)


def structured(res):
    data = res.structured_content
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


def test_tools_are_listed_with_schemas_and_annotations(sources_path):
    async def go():
        async with Client(build_server(lambda a, c: FakeMail(), sources_path)) as c:
            return (await c.list_tools()).tools
    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == {"list_sources", "fetch_source", "suggest_newsletter_filter", "subscribe_source", "unsubscribe_source",
                          "run_ingest", "ingest_items", "extract_entities", "top_movers", "trend_metrics",
                          "emerging_clusters", "reading_list", "semantic_search"}
    assert tools["top_movers"].annotations.read_only_hint is True
    assert tools["run_ingest"].annotations.read_only_hint is False
    fetch = tools["fetch_source"]
    assert fetch.input_schema["required"] == ["source_id"] and fetch.output_schema
    assert fetch.annotations.read_only_hint is True
    assert tools["unsubscribe_source"].annotations.destructive_hint is True
    assert "Read-only" in fetch.description


def test_fetch_source_returns_structured_items(sources_path):
    res = call(build_server(lambda a, c: FakeMail(), sources_path), "fetch_source", {"source_id": "tldr_ai"})
    assert not res.is_error
    data = structured(res)
    assert (data["scanned"], data["matched"], data["item_count"]) == (3, 2, 4)
    assert data["items"][0]["url"] == "https://www.example-lab.ai/research/new-model"


def test_auth_failure_reaches_the_model_as_a_readable_tool_error(sources_path):
    server = build_server(lambda a, c: FakeMail(MailAuthRequired("run `signal-radar auth work`")), sources_path)
    res = call(server, "fetch_source", {"source_id": "tldr_ai"})
    assert res.is_error and "signal-radar auth work" in res.content[0].text


def test_unknown_source_is_a_tool_error(sources_path):
    res = call(build_server(lambda a, c: FakeMail(), sources_path), "fetch_source", {"source_id": "nope"})
    assert res.is_error and "unknown source" in res.content[0].text


def test_suggest_filter_reads_headers_only(sources_path):
    res = call(build_server(lambda a, c: FakeMail(), sources_path), "suggest_newsletter_filter",
               {"account": "work", "folder": "Newsletters/TLDR"})
    groups = structured(res)
    assert groups[0]["from_name"] == "TLDR AI" and groups[0]["proposed_match"]["from_name"] == ["TLDR AI"]


def test_subscribe_and_unsubscribe_round_trip(sources_path):
    server = build_server(lambda a, c: FakeMail(), sources_path)
    res = call(server, "subscribe_source", {
        "source_id": "tldr_infosec", "type": "email_newsletter", "account": "work", "folder": "Newsletters/TLDR",
        "match": {"from": ["@tldrnewsletter.com"], "from_name": ["TLDR InfoSec"]}, "tags": ["security"],
    })
    assert structured(res)["action"] == "subscribed"
    assert any(s["id"] == "tldr_infosec" for s in yaml.safe_load(sources_path.read_text())["sources"])

    assert structured(call(server, "unsubscribe_source", {"source_id": "tldr_infosec"}))["action"] == "disabled"
    assert structured(call(server, "subscribe_source", {"source_id": "tldr_infosec", "type": "email_newsletter"}))["action"] == "enabled"
    assert structured(call(server, "unsubscribe_source", {"source_id": "tldr_infosec", "purge": True}))["action"] == "removed"
    assert not any(s["id"] == "tldr_infosec" for s in yaml.safe_load(sources_path.read_text())["sources"])


def test_subscribe_rejects_bad_input_without_touching_the_file(sources_path):
    before = sources_path.read_text()
    server = build_server(lambda a, c: FakeMail(), sources_path)
    res = call(server, "subscribe_source", {"source_id": "x", "type": "email_newsletter", "account": "ghost",
                                            "match": {"from": ["a"]}})
    assert res.is_error and "unknown mail account" in res.content[0].text
    assert call(server, "subscribe_source", {"source_id": "y", "type": "email_newsletter"}).is_error
    assert sources_path.read_text() == before
