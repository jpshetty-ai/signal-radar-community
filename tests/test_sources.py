from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from signal_radar.config import EmailMatch, EmailNewsletterSource, SourcesConfig, load_sources
from signal_radar.sources import EmailNewsletterAdapter, GraphMailClient, MailAuthRequired, MailError, MailMessage, sender_groups

FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "tldr_ai_synthetic.html").read_text()
SINCE = datetime(2026, 10, 1, tzinfo=UTC)


def msg(name, subject="Headlines", address="dan@tldrnewsletter.com", html=FIXTURE_HTML, i=0):
    return MailMessage(id=f"m{i}", internet_message_id=f"<m{i}@x>", subject=subject, from_address=address,
                       from_name=name, received_at=datetime(2026, 10, 2, 12, i, tzinfo=UTC), html=html)


class FakeMail:
    def __init__(self, messages):
        self.messages, self.calls = messages, []

    def list_messages(self, folder, since, with_body=True, senders=None):
        self.calls.append((folder, since, with_body))
        return iter(self.messages)


# ------------------------------------------------------------------ matching


@pytest.mark.parametrize("name,expected", [("TLDR AI", True), ("tldr ai ", True), ("TLDR", False), ("TLDR AI Daily", False)])
def test_from_name_is_exact_case_insensitive(name, expected):
    m = EmailMatch.model_validate({"from": ["@tldrnewsletter.com"], "from_name": ["TLDR AI"]})
    assert m.matches("dan@tldrnewsletter.com", name, "anything") is expected


def test_subject_rules_and_address_substring():
    m = EmailMatch.model_validate({"from": ["example.com"], "subject_contains": ["Weekly"], "subject_excludes": ["Ad"]})
    assert m.matches("news@example.com", "", "The Weekly")
    assert not m.matches("news@example.com", "", "Weekly Ad")
    assert not m.matches("news@other.com", "", "The Weekly")


def test_repo_sources_use_graph_account_and_editions_are_distinct():
    cfg = load_sources()
    assert cfg.mail_accounts["work"].kind == "graph"
    rules = [(tuple(s.match.from_), tuple(s.match.from_name), tuple(s.match.subject_contains))
             for s in cfg.sources if isinstance(s, EmailNewsletterSource)]
    assert len(rules) == len(set(rules)), "two sources share a match rule and would ingest the same issues"


# ------------------------------------------------------------------ adapter


def tldr_source(from_name="TLDR AI"):
    return EmailNewsletterSource.model_validate({
        "id": "tldr_ai", "type": "email_newsletter", "account": "work", "folder": "Newsletters/TLDR",
        "match": {"from": ["@tldrnewsletter.com"], "from_name": [from_name]}, "parser": "tldr", "tags": ["ai"],
    })


def test_adapter_keeps_only_matching_editions_and_dedupes_across_messages():
    mail = FakeMail([msg("TLDR AI", i=0), msg("TLDR", i=1), msg("TLDR AI", i=2)])
    report = EmailNewsletterAdapter(tldr_source(), mail).fetch_report(SINCE)
    assert (report.scanned, report.matched) == (3, 2)
    assert len(report.items) == 4  # same issue twice -> items deduped by id
    assert all(i.fetched_at and i.source_id == "tldr_ai" for i in report.items)
    assert mail.calls[0][:2] == ("Newsletters/TLDR", SINCE)
    assert not report.warnings


def test_adapter_warns_when_nothing_matches_or_parser_finds_nothing():
    report = EmailNewsletterAdapter(tldr_source("TLDR Nope"), FakeMail([msg("TLDR AI")])).fetch_report(SINCE)
    assert report.matched == 0 and "none matched" in report.warnings[0]
    report = EmailNewsletterAdapter(tldr_source(), FakeMail([msg("TLDR AI", html="<p>new template</p>")])).fetch_report(SINCE)
    assert report.matched == 1 and "no items" in report.warnings[0]


def test_sender_groups_propose_display_name_only_when_address_is_shared():
    groups = sender_groups([msg("TLDR AI", i=0), msg("TLDR", i=1), msg("TLDR AI", i=2),
                            msg("Simon", address="simon@simonwillison.net", i=3)])
    by_name = {g["from_name"]: g for g in groups}
    assert by_name["TLDR AI"]["count"] == 2 and groups[0]["from_name"] == "TLDR AI"
    assert by_name["TLDR AI"]["proposed_match"] == {"from": ["@tldrnewsletter.com"], "from_name": ["TLDR AI"]}
    assert by_name["Simon"]["proposed_match"] == {"from": ["@simonwillison.net"]}


# ------------------------------------------------------------- graph client


def graph(handler):
    return GraphMailClient(lambda: "tok", http=httpx.Client(transport=httpx.MockTransport(handler)))


def message_json(i):
    return {"id": f"id{i}", "internetMessageId": f"<{i}@x>", "subject": f"s{i}",
            "from": {"emailAddress": {"name": "TLDR AI", "address": "dan@tldrnewsletter.com"}},
            "receivedDateTime": "2026-10-02T13:57:21Z", "body": {"contentType": "html", "content": "<p>x</p>"}}


def test_graph_resolves_slash_named_folder_then_pages_messages():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        assert req.headers["Authorization"] == "Bearer tok"
        p = req.url.path
        if p == "/v1.0/me/mailFolders":
            assert req.url.params["$filter"] == "displayName eq 'Newsletters/TLDR'"
            return httpx.Response(200, json={"value": [{"id": "F1", "displayName": "Newsletters/TLDR"}]})
        if p == "/v1.0/me/mailFolders/F1/messages" and "$skip" not in str(req.url):
            assert req.url.params["$filter"] == "receivedDateTime ge 2026-10-01T00:00:00Z"
            return httpx.Response(200, json={"value": [message_json(1)],
                                             "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/F1/messages?$skip=1"})
        return httpx.Response(200, json={"value": [message_json(2)]})

    c = graph(handler)
    msgs = list(c.list_messages("Newsletters/TLDR", SINCE))
    assert [m.id for m in msgs] == ["id1", "id2"]
    assert msgs[0].from_name == "TLDR AI" and msgs[0].received_at.tzinfo is not None and msgs[0].html == "<p>x</p>"
    list(c.list_messages("Newsletters/TLDR", SINCE))
    assert sum(r.url.path == "/v1.0/me/mailFolders" for r in seen) == 1  # folder id cached


def test_graph_walks_nested_folder_path_and_escapes_quotes():
    def handler(req: httpx.Request):
        f = req.url.params.get("$filter", "")
        if req.url.path == "/v1.0/me/mailFolders" and f == "displayName eq 'Bob''s/News'":
            return httpx.Response(200, json={"value": []})
        if req.url.path == "/v1.0/me/mailFolders" and f == "displayName eq 'Bob''s'":
            return httpx.Response(200, json={"value": [{"id": "P"}]})
        if req.url.path == "/v1.0/me/mailFolders/P/childFolders":
            return httpx.Response(200, json={"value": [{"id": "C"}]})
        return httpx.Response(404, json={"error": {"code": "x", "message": "unexpected " + str(req.url)}})

    assert graph(handler).resolve_folder("Bob's/News") == "C"
    assert graph(handler).resolve_folder("Inbox") == "inbox"


def test_graph_errors_are_typed_and_throttling_retried(monkeypatch):
    monkeypatch.setattr("signal_radar.sources.m365.time.sleep", lambda s: None)
    calls = {"n": 0}

    def throttled(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"value": [{"id": "F"}]})

    assert graph(throttled).resolve_folder("X") == "F" and calls["n"] == 2
    with pytest.raises(MailAuthRequired):
        graph(lambda r: httpx.Response(401)).resolve_folder("X")
    with pytest.raises(MailError, match="not found"):
        graph(lambda r: httpx.Response(200, json={"value": []})).resolve_folder("Missing")
    with pytest.raises(MailError, match="ErrorAccessDenied"):
        graph(lambda r: httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "no"}})).resolve_folder("X")


def test_unknown_mail_account_is_rejected():
    src = {"id": "x", "type": "email_newsletter", "account": "nope", "match": {"from": ["a"]}}
    with pytest.raises(ValueError, match="unknown mail account"):
        SourcesConfig.model_validate({"sources": [src]})


# ---------------------------------------------------------------- imap client

from email.message import EmailMessage  # noqa: E402

from signal_radar.sources import ImapMailClient  # noqa: E402


def raw_email(name, addr, subject, html):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = f"{name} <{addr}>", subject, f"<{subject}@x>"
    m.set_content("plain fallback")
    m.add_alternative(html, subtype="html")
    return m.as_bytes()


class FakeImap:
    """Just enough of imaplib.IMAP4 for ImapMailClient, recording every command."""

    def __init__(self, messages, password="right"):
        self.messages, self.password, self.log = messages, password, []

    def login(self, user, password):
        import imaplib
        self.log.append(("login", user))
        if password != self.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    def select(self, folder, readonly=False):
        self.log.append(("select", folder, readonly))
        return ("OK", [b"3"]) if folder == '"Newsletters"' else ("NO", [b"no such folder"])

    def uid(self, cmd, *args):
        self.log.append(("uid", cmd) + args)
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in self.messages)]
        uids = args[0].split(",")
        rows = []
        for u in uids:
            when, raw = self.messages[int(u)]
            rows += [(f'{u} (UID {u} INTERNALDATE "{when}" BODY[] {{{len(raw)}}}'.encode(), raw), b")"]
        return "OK", rows

    def logout(self):
        self.log.append(("logout",))


def imap_client(fake, password="right"):
    return ImapMailClient("imap.example.com", "me@example.com", password, connect=lambda h, p: fake)


def test_imap_reads_read_only_without_marking_seen_and_cuts_on_internaldate():
    fake = FakeImap({
        1: ("30-Sep-2026 09:00:00 +0000", raw_email("AlphaSignal", "news@alphasignal.ai", "old", "<p>old</p>")),
        2: ("02-Oct-2026 14:29:44 +0000", raw_email("AlphaSignal", "news@alphasignal.ai", "new", "<p>new</p>")),
        3: ("01-Oct-2026 14:07:38 +0000", raw_email("TLDR AI", "dan@tldrnewsletter.com", "mid", "<p>mid</p>")),
    })
    msgs = list(imap_client(fake).list_messages("Newsletters", SINCE))
    assert [m.subject for m in msgs] == ["new", "mid"]  # newest first; 30 Sep is before SINCE
    assert (msgs[0].from_name, msgs[0].from_address) == ("AlphaSignal", "news@alphasignal.ai")
    assert msgs[0].html.strip() == "<p>new</p>" and msgs[0].received_at == datetime(2026, 10, 2, 14, 29, 44, tzinfo=UTC)
    assert ("select", '"Newsletters"', True) in fake.log                     # EXAMINE, not SELECT
    assert ("uid", "SEARCH", None, "SINCE", "30-Sep-2026") in fake.log     # one day of slack
    fetches = [e for e in fake.log if e[:2] == ("uid", "FETCH")]
    assert fetches and all("BODY.PEEK[]" in e[3] for e in fetches)        # PEEK: never sets \Seen
    assert fake.log[-1] == ("logout",)


def test_imap_headers_only_and_typed_failures():
    fake = FakeImap({1: ("02-Oct-2026 14:29:44 +0000", raw_email("A", "a@b.c", "s", "<p>x</p>"))})
    msgs = list(imap_client(fake).list_messages("Newsletters", SINCE, with_body=False))
    assert msgs[0].html is None and "BODY.PEEK[HEADER.FIELDS" in [e for e in fake.log if e[:2] == ("uid", "FETCH")][0][3]
    with pytest.raises(MailAuthRequired, match="app password"):
        list(imap_client(FakeImap({}), password="wrong").list_messages("Newsletters", SINCE))
    with pytest.raises(MailError, match="not found"):
        list(imap_client(FakeImap({})).list_messages("Missing", SINCE))
