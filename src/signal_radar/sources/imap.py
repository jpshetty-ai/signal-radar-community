"""IMAP mail reader, used for Gmail with an app password (ADR-0008).

Read-only by construction:
- folders are opened with EXAMINE (`select(readonly=True)`), so the server refuses flag changes, moves, deletes
- bodies are fetched with BODY.PEEK[], which does not set \\Seen, so newsletters stay unread in Gmail

Gmail exposes labels as IMAP folders, so the label "Newsletters" is the folder "Newsletters".
IMAP SEARCH SINCE has day granularity; the exact `since` cut is applied on INTERNALDATE after fetching.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email import policy
from email.utils import parseaddr
from typing import Any

from ..config import ImapAccount
from .m365 import MailAuthRequired, MailError, MailMessage

_HEADERS_ONLY = "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])"
_FULL = "(INTERNALDATE BODY.PEEK[])"
_BATCH = 25


def imap_password(account: ImapAccount, user: str) -> str:
    password = None
    if account.keyring_service:
        import keyring

        password = keyring.get_password(account.keyring_service, user)
    if not password and account.password_env:
        password = os.environ.get(account.password_env)
    if not password:
        raise MailAuthRequired(
            f"no IMAP password for {user}: store it with "
            f"`security add-generic-password -s {account.keyring_service or 'signal-radar-imap'} -a {user} -w`"
        )
    return password.replace(" ", "")  # Google shows app passwords in groups of four


def _from_criteria(senders: list[str]) -> list[str]:
    """IMAP search terms for "from any of these": FROM a | OR FROM a FROM b | OR FROM a OR FROM b FROM c ..."""
    terms = [f'FROM "{s.replace(chr(34), "")}"' for s in senders if s.strip()]
    if not terms:
        return []
    crit = terms[-1]
    for t in reversed(terms[:-1]):
        crit = f"OR {t} {crit}"
    return [crit]


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode_message(raw: bytes, internaldate: datetime, uid: str, with_body: bool) -> MailMessage:
    msg = email.message_from_bytes(raw, policy=policy.default)
    name, address = parseaddr(str(msg.get("From", "")))
    html = None
    if with_body:
        part = msg.get_body(preferencelist=("html", "plain"))
        html = part.get_content() if part is not None else ""
    return MailMessage(
        id=uid,
        internet_message_id=str(msg.get("Message-ID", "")).strip() or None,
        subject=str(msg.get("Subject", "")),
        from_address=address,
        from_name=name,
        received_at=internaldate,
        html=html,
    )


class ImapMailClient:
    def __init__(self, host: str, user: str, password: str | Callable[[], str], port: int = 993,
                 connect: Callable[[str, int], Any] | None = None):
        self.host, self.port, self.user = host, port, user
        self._password = password
        self._connect = connect or (lambda h, p: imaplib.IMAP4_SSL(h, p, timeout=60))

    @classmethod
    def from_account(cls, account: ImapAccount) -> ImapMailClient:
        user = os.environ.get(account.user_env, "").strip()
        if not user:
            raise MailError(f"environment variable {account.user_env} is not set (see .env.example)")
        return cls(account.host, user, lambda: imap_password(account, user), account.port)

    def _login(self):
        conn = self._connect(self.host, self.port)
        password = self._password() if callable(self._password) else self._password
        try:
            conn.login(self.user, password)
        except imaplib.IMAP4.error as e:
            raise MailAuthRequired(f"IMAP login to {self.host} as {self.user} failed ({e}); check the app password") from e
        return conn

    def check_login(self) -> None:
        conn = self._login()
        conn.logout()

    def list_messages(self, folder: str, since: datetime, with_body: bool = True,
                      senders: list[str] | None = None) -> Iterator[MailMessage]:
        """`senders`: address substrings searched on the server (IMAP FROM), so a large folder such as All Mail
        is not downloaded whole. The adapter still applies the full match rule to what comes back."""
        since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        conn = self._login()
        try:
            status, _ = conn.select(_quote(folder), readonly=True)
            if status != "OK":
                raise MailError(f"IMAP folder {folder!r} not found on {self.host}")
            # SINCE is a date in the server's view; go one day earlier and cut exactly below.
            day = (since_utc - timedelta(days=1)).strftime("%d-%b-%Y")
            status, data = conn.uid("SEARCH", None, "SINCE", day, *_from_criteria(senders or []))
            if status != "OK":
                raise MailError(f"IMAP search failed in {folder!r}: {data}")
            uids = data[0].split() if data and data[0] else []
            messages: list[MailMessage] = []
            for i in range(0, len(uids), _BATCH):
                batch = b",".join(uids[i:i + _BATCH]).decode()
                status, rows = conn.uid("FETCH", batch, _FULL if with_body else _HEADERS_ONLY)
                if status != "OK":
                    raise MailError(f"IMAP fetch failed: {rows}")
                for row in rows:
                    if not isinstance(row, tuple):
                        continue  # the b")" closing each FETCH response
                    meta, raw = row
                    uid = re.search(rb"UID (\d+)", meta)
                    when = imaplib.Internaldate2tuple(meta)
                    if uid is None or when is None:
                        continue
                    # Internaldate2tuple returns local time; mktime turns it back into an epoch.
                    received = datetime.fromtimestamp(time.mktime(when), tz=UTC)
                    if received < since_utc:
                        continue
                    messages.append(_decode_message(raw, received, uid.group(1).decode(), with_body))
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001 - logout failure must not hide the real error
                pass
        messages.sort(key=lambda m: m.received_at, reverse=True)
        yield from messages
