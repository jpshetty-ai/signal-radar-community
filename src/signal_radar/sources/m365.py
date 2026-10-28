"""Microsoft 365 mail through Microsoft Graph, delegated Mail.Read (ADR-0007).

Auth: MSAL public client + device code flow. `signal-radar auth <account>` signs in once in a browser;
after that the refresh token in the macOS keychain lets ingest run unattended until it expires
(90 days sliding, or sooner if tenant policy says so). Nothing here ever sends mail or changes the mailbox.

The HTTP side takes a token *provider* rather than doing auth itself, so tests run on httpx.MockTransport
without MSAL, the keychain, or the network.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..config import DATA_DIR, GraphAccount

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read"]  # MSAL adds openid/profile/offline_access itself
_WELL_KNOWN = {"inbox", "archive", "drafts", "sentitems", "deleteditems", "junkemail"}


class MailError(RuntimeError):
    pass


class MailAuthRequired(MailError):
    """No usable cached token: a human has to run the device code sign-in."""


@dataclass
class MailMessage:
    id: str
    internet_message_id: str | None
    subject: str
    from_address: str
    from_name: str
    received_at: datetime
    html: str | None  # None when listed without body


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MailError(f"environment variable {name} is not set (see .env.example)")
    return value


class GraphAuth:
    def __init__(self, account_name: str, account: GraphAccount, cache_dir: Path | None = None):
        import msal
        from msal_extensions import KeychainPersistence, PersistedTokenCache

        self.account_name = account_name
        self.user = _env(account.user_env)
        tenant, client_id = _env(account.tenant_id_env), _env(account.client_id_env)
        cache_dir = cache_dir or DATA_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        # The keychain holds the tokens; the signal file is only a change marker msal-extensions uses for locking.
        persistence = KeychainPersistence(
            str(cache_dir / f"msal_{account_name}.signal"),
            service_name=account.keychain_service,
            account_name=self.user,
        )
        self.app = msal.PublicClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant}",
            token_cache=PersistedTokenCache(persistence),
        )

    def token(self) -> str:
        for acct in self.app.get_accounts(username=self.user):
            result = self.app.acquire_token_silent(SCOPES, account=acct)
            if result and "access_token" in result:
                return result["access_token"]
        raise MailAuthRequired(
            f"mail account {self.account_name!r} ({self.user}) needs a sign-in: run `signal-radar auth {self.account_name}`"
        )

    def login(self, echo: Callable[[str], None] = print) -> str:
        """Interactive device code flow. Returns the signed-in username."""
        flow = self.app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise MailError(f"device flow failed to start: {flow.get('error_description', flow)}")
        echo(flow["message"])
        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise MailError(f"sign-in failed: {result.get('error')}: {result.get('error_description')}")
        username = result.get("id_token_claims", {}).get("preferred_username", "")
        if username and username.lower() != self.user.lower():
            raise MailError(f"signed in as {username}, but the account is configured for {self.user}")
        return username or self.user

    def logout(self) -> None:
        for acct in self.app.get_accounts(username=self.user):
            self.app.remove_account(acct)


class GraphMailClient:
    def __init__(self, token_provider: Callable[[], str], http: httpx.Client | None = None, max_retries: int = 3):
        self._token = token_provider
        self._http = http or httpx.Client(timeout=60)
        self._max_retries = max_retries
        self._folder_ids: dict[str, str] = {}

    # ------------------------------------------------------------- http

    def _get(self, url: str, params: dict[str, str] | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}", "Prefer": 'outlook.body-content-type="html"'}
        for attempt in range(self._max_retries + 1):
            resp = self._http.get(url, params=params, headers=headers)
            if resp.status_code in (429, 503, 504) and attempt < self._max_retries:
                time.sleep(min(float(resp.headers.get("Retry-After", 2 ** attempt)), 30))
                continue
            if resp.status_code == 401:
                raise MailAuthRequired("Graph rejected the token (401); sign in again with `signal-radar auth`")
            if resp.status_code >= 400:
                err = resp.json().get("error", {}) if "json" in resp.headers.get("content-type", "") else {}
                raise MailError(f"Graph {resp.status_code} on {url}: {err.get('code')}: {err.get('message', resp.text[:200])}")
            return resp.json()
        raise MailError(f"Graph kept throttling {url}")

    def _pages(self, url: str, params: dict[str, str] | None) -> Iterator[dict]:
        page = self._get(url, params)
        while True:
            yield from page.get("value", [])
            nxt = page.get("@odata.nextLink")
            if not nxt:
                return
            page = self._get(nxt)  # nextLink already carries the query

    # ---------------------------------------------------------- folders

    def _find_child(self, parent_id: str | None, name: str) -> str | None:
        base = f"{GRAPH}/me/mailFolders" + (f"/{parent_id}/childFolders" if parent_id else "")
        escaped = name.replace("'", "''")
        for f in self._pages(base, {"$filter": f"displayName eq '{escaped}'", "$select": "id,displayName"}):
            return f["id"]
        return None

    def resolve_folder(self, path: str) -> str:
        """Folder id for 'Inbox', a top-level name (which may itself contain '/'), or a nested 'A/B' path."""
        if path in self._folder_ids:
            return self._folder_ids[path]
        if path.replace(" ", "").lower() in _WELL_KNOWN:
            folder_id = path.replace(" ", "").lower()
        else:
            # Outlook allows '/' inside a folder name, so try the whole string first, then walk segments.
            folder_id = self._find_child(None, path)
            if folder_id is None and "/" in path:
                parent: str | None = None
                for seg in path.split("/"):
                    parent = self._find_child(parent, seg)
                    if parent is None:
                        break
                folder_id = parent
        if folder_id is None:
            raise MailError(f"mail folder {path!r} not found")
        self._folder_ids[path] = folder_id
        return folder_id

    # --------------------------------------------------------- messages

    def list_messages(self, folder: str, since: datetime, with_body: bool = True,
                      senders: list[str] | None = None) -> Iterator[MailMessage]:
        """Messages received at or after `since`, newest first. Read-only. `senders` is accepted for interface
        parity and ignored: the adapter's match rule filters (folders here are small)."""
        since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        fields = "id,internetMessageId,subject,from,receivedDateTime" + (",body" if with_body else "")
        params = {
            "$filter": f"receivedDateTime ge {since_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime desc",
            "$select": fields,
            "$top": "25",
        }
        url = f"{GRAPH}/me/mailFolders/{self.resolve_folder(folder)}/messages"
        for m in self._pages(url, params):
            sender = (m.get("from") or {}).get("emailAddress") or {}
            yield MailMessage(
                id=m["id"],
                internet_message_id=m.get("internetMessageId"),
                subject=m.get("subject") or "",
                from_address=sender.get("address") or "",
                from_name=sender.get("name") or "",
                received_at=datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")),
                html=(m.get("body") or {}).get("content") if with_body else None,
            )
