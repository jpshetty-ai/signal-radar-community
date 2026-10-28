"""Signals a parser can raise instead of returning items."""


class SkipMessage(Exception):
    """This email belongs to the source but is not a news issue (e.g. a weekend essay). Not an error."""


class ParseDeferred(Exception):
    """The issue can't be parsed correctly right now (e.g. link resolution failed). Retry on a later run;
    don't store a degraded version, because a later correct parse would get different item ids."""
