"""Newsletter parsers: HTML email body -> list[Item]. Selected per source by `parser:` in sources.yaml."""

from __future__ import annotations

from collections.abc import Callable

from ...items import Item
from .alphasignal import parse_alphasignal
from .bayarealetters import parse_bayarealetters
from .common import ParseDeferred, SkipMessage
from .tldr import parse_tldr

Parser = Callable[..., list[Item]]  # (html, *, source_id, received_at, origin_ref, tags) -> items

PARSERS: dict[str, Parser] = {"tldr": parse_tldr, "alphasignal": parse_alphasignal,
                              "bayarealetters": parse_bayarealetters}


def get_parser(name: str) -> Parser:
    try:
        return PARSERS[name]
    except KeyError:
        raise ValueError(f"unknown parser {name!r}; available: {sorted(PARSERS)}") from None


__all__ = ["PARSERS", "ParseDeferred", "Parser", "SkipMessage", "get_parser", "parse_alphasignal", "parse_bayarealetters", "parse_tldr"]
