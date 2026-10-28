"""Source adapters. `build_adapter` turns a sources.yaml entry into something with fetch(since)."""

from __future__ import annotations

from collections.abc import Callable

from ..config import EmailNewsletterSource, FeedSource, GraphAccount, ImapAccount, Source, SourcesConfig
from .base import AdapterBase, FetchReport, SourceAdapter
from .email_newsletter import EmailNewsletterAdapter, MailReader, sender_groups
from .feed import FeedAdapter, FeedError
from .imap import ImapMailClient
from .m365 import GraphAuth, GraphMailClient, MailAuthRequired, MailError, MailMessage

MailFactory = Callable[[str, SourcesConfig], MailReader]


class SourceNotSupported(RuntimeError):
    pass


def default_mail_factory(account_name: str, cfg: SourcesConfig) -> MailReader:
    account = cfg.mail_accounts[account_name]
    if isinstance(account, GraphAccount):
        auth = GraphAuth(account_name, account)
        return GraphMailClient(auth.token)
    if isinstance(account, ImapAccount):
        return ImapMailClient.from_account(account)
    raise SourceNotSupported(f"mail account kind {account.kind!r} has no reader")


def build_adapter(source: Source, cfg: SourcesConfig, mail_factory: MailFactory = default_mail_factory) -> SourceAdapter:
    if isinstance(source, EmailNewsletterSource):
        return EmailNewsletterAdapter(source, mail_factory(source.account, cfg))
    if isinstance(source, FeedSource):
        return FeedAdapter(source)
    raise SourceNotSupported(f"source type {source.type!r} is planned for Phase 6")


__all__ = [
    "AdapterBase", "EmailNewsletterAdapter", "FeedAdapter", "FeedError", "FetchReport", "GraphAuth", "GraphMailClient", "ImapMailClient", "MailAuthRequired",
    "MailError", "MailFactory", "MailMessage", "MailReader", "SourceAdapter", "SourceNotSupported",
    "build_adapter", "default_mail_factory", "sender_groups",
]
