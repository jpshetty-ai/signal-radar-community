"""Typed loaders for config/models.yaml and config/sources.yaml.

Validation happens at load time so a typo in YAML fails fast with a clear message,
not halfway through a nightly ingest.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

PROJECT_ROOT = Path(os.environ.get("SIGNAL_RADAR_HOME", Path(__file__).resolve().parents[2]))
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"

Role = Literal["extraction", "reasoning", "writing", "embedding"]
ROLES: tuple[Role, ...] = ("extraction", "reasoning", "writing", "embedding")
ProviderKind = Literal["ollama", "anthropic", "openai", "fastembed"]  # "openai" = any OpenAI-compatible API (OpenRouter, DeepSeek, vLLM)
PROVIDER_KINDS: tuple[str, ...] = ("ollama", "anthropic", "openai", "fastembed")
Provider = str  # a name under `providers:` in models.yaml, or a kind used directly


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- models.yaml


class Price(_Strict):
    input: float
    output: float
    cache_read: float | None = None


class RoleSpec(_Strict):
    """Either one concrete model (provider + model) or a ranked list of catalog candidates (ADR-0013).

    With candidates, the router tries them in the order the strategy gives and falls through on failure:
    - first_available: the listed order
    - cheapest_passing: candidates whose last evaluated quality for this role >= min_quality, cheapest first,
      then the rest in listed order as fallbacks (data/model_eval.json, written by `signal-radar models eval`)
    """

    provider: Provider | None = None
    model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    candidates: list[str] = Field(default_factory=list)
    strategy: Literal["first_available", "cheapest_passing"] = "first_available"
    min_quality: float = Field(0.8, ge=0, le=1)

    @model_validator(mode="after")
    def _one_form(self) -> RoleSpec:
        concrete = bool(self.provider and self.model)
        if concrete == bool(self.candidates):
            raise ValueError("a role needs either provider + model, or candidates (not both)")
        return self

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}" if self.provider else "candidates:" + ",".join(self.candidates)


class CatalogEntry(_Strict):
    """A named model choice: where it is served, what it costs, and what happens to the data."""

    provider: Provider
    model: str
    params: dict[str, Any] = Field(default_factory=dict)
    price: Price | None = None
    data_policy: str = ""  # e.g. "OpenRouter, no-retention providers only"
    notes: str = ""


class JudgeSpec(_Strict):
    """A non-chat judge model for evaluations (ADR-0013), e.g. TypeSafe Jev: typed judgments, priced per input token."""

    kind: Literal["typesafe"]
    model: str
    api_key_env: str
    price_input_per_million: float


class Budget(_Strict):
    """Monthly cap on paid calls. When reached, paid candidates are skipped and the role falls through
    to the next (usually local) candidate. Enforces the >$10/month approval rule in code."""

    monthly_usd: float | None = None


class ProviderSettings(_Strict):
    kind: ProviderKind | None = None  # defaults to the provider's name ("ollama", "anthropic", "openai")
    host_env: str | None = None
    default_host: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    timeout_s: float = 600
    extra_body: dict[str, Any] = Field(default_factory=dict)  # e.g. OpenRouter provider routing / data policy
    headers: dict[str, str] = Field(default_factory=dict)


class ModelsConfig(_Strict):
    roles: dict[Role, RoleSpec]
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    pricing: dict[str, Price] = Field(default_factory=dict)
    catalog: dict[str, CatalogEntry] = Field(default_factory=dict)
    judges: dict[str, JudgeSpec] = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def _check(self) -> ModelsConfig:
        missing = set(ROLES) - set(self.roles)
        if missing:
            raise ValueError(f"models.yaml is missing roles: {sorted(missing)}")
        for name, p in self.providers.items():
            if p.kind is None and name not in PROVIDER_KINDS:
                raise ValueError(f"provider {name!r} needs a kind ({', '.join(PROVIDER_KINDS)})")
        known = set(self.providers) | set(PROVIDER_KINDS)
        for role, spec in self.roles.items():
            if spec.provider and spec.provider not in known:
                raise ValueError(f"role {role}: unknown provider {spec.provider!r}")
            for c in spec.candidates:
                if c not in self.catalog:
                    raise ValueError(f"role {role}: candidate {c!r} is not in the catalog")
        for cid, entry in self.catalog.items():
            if entry.provider not in known:
                raise ValueError(f"catalog {cid}: unknown provider {entry.provider!r}")
        return self

    def kind_of(self, provider: str) -> str:
        p = self.providers.get(provider)
        return (p.kind if p and p.kind else provider)

    def price_for(self, provider: str, model: str) -> Price | None:
        """Catalog entry first, then exact 'provider/model', then 'provider/*'. None means unknown."""
        for e in self.catalog.values():
            if e.provider == provider and e.model == model and e.price is not None:
                return e.price
        return self.pricing.get(f"{provider}/{model}") or self.pricing.get(f"{provider}/*")

    def concrete(self, cid: str, role_params: dict[str, Any] | None = None) -> RoleSpec:
        e = self.catalog[cid]
        return RoleSpec(provider=e.provider, model=e.model, params={**(role_params or {}), **e.params})

    def resolve(self, role: Role, scores: dict | None = None) -> list[tuple[str, RoleSpec]]:
        """Ordered (label, concrete spec) list the router will try for a role."""
        spec = self.roles[role]
        if spec.provider:
            return [(spec.key, spec)]
        order = list(spec.candidates)
        if spec.strategy == "cheapest_passing" and scores:
            role_scores = scores.get(role, {})
            passing = [c for c in order if role_scores.get(c, {}).get("quality", 0) >= spec.min_quality]

            def cost(c: str) -> float:
                measured = role_scores.get(c, {}).get("cost_per_task")
                if measured is not None:
                    return measured
                p = self.catalog[c].price
                return (p.input * 3 + p.output) / 4 if p else float("inf")

            passing.sort(key=cost)
            order = passing + [c for c in order if c not in passing]
        return [(c, self.concrete(c, spec.params)) for c in order]

    def primary(self, role: Role, scores: dict | None = None) -> RoleSpec:
        return self.resolve(role, scores)[0][1]

    def with_overrides(self, overrides: dict[Role, RoleSpec]) -> ModelsConfig:
        roles = {**self.roles, **overrides}
        return self.model_copy(update={"roles": roles})


def parse_override(text: str) -> tuple[Role, RoleSpec]:
    """Parse a CLI override 'role=provider/model' or 'role=@catalog_id'. Only the first '/' splits provider from model."""
    role, sep, target = text.partition("=")
    if sep and target.startswith("@"):
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        return role, RoleSpec(candidates=[target[1:]])  # type: ignore[return-value]
    provider, slash, model = target.partition("/")
    if not sep or not slash or not model:
        raise ValueError(f"expected role=provider/model, got {text!r}")
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
    return role, RoleSpec(provider=provider, model=model)  # type: ignore[arg-type]


def load_models(path: Path | None = None, overrides: list[str] | None = None) -> ModelsConfig:
    path = path or CONFIG_DIR / "models.yaml"
    cfg = ModelsConfig.model_validate(yaml.safe_load(path.read_text()))
    if overrides:
        parsed: dict[Role, RoleSpec] = {}
        for item in overrides:
            role, spec = parse_override(item)
            # Keep the role's params only if the provider is unchanged; provider-specific
            # params (e.g. ollama 'think', anthropic 'effort') would be wrong elsewhere.
            base = cfg.roles[role]
            if spec.provider and base.provider == spec.provider:
                spec = spec.model_copy(update={"params": base.params})
            parsed[role] = spec
        known = set(cfg.providers) | set(PROVIDER_KINDS)
        for role, spec in parsed.items():
            if spec.provider and spec.provider not in known:
                raise ValueError(f"--model {role}: unknown provider {spec.provider!r} (known: {', '.join(sorted(known))})")
            unknown = [cand for cand in spec.candidates if cand not in cfg.catalog]
            if unknown:
                raise ValueError(f"--model {role}=@{unknown[0]}: not in the catalog ({', '.join(sorted(cfg.catalog))})")
        cfg = cfg.with_overrides(parsed)
    return cfg


# --------------------------------------------------------------- sources.yaml


class ImapAccount(_Strict):
    """IMAP mailbox, e.g. Gmail with an app password (ADR-0008). Opened read-only (EXAMINE) by the reader.

    The password comes from the OS keychain (service = keyring_service, account = the user address);
    password_env is a fallback for headless runs without a keychain.
    """

    kind: Literal["imap"]
    host: str                      # not a secret, e.g. imap.gmail.com
    user_env: str
    keyring_service: str | None = None
    password_env: str | None = None
    port: int = 993

    @model_validator(mode="after")
    def _has_password_source(self) -> ImapAccount:
        if not (self.keyring_service or self.password_env):
            raise ValueError("imap account needs keyring_service or password_env")
        return self


class GraphAccount(_Strict):
    """Microsoft 365 mailbox read through Microsoft Graph with delegated Mail.Read (ADR-0007).

    Only env var *names* live here. The client id is not a secret, but tenant ids and addresses stay out of Git.
    """

    kind: Literal["graph"]
    tenant_id_env: str
    client_id_env: str
    user_env: str
    keychain_service: str = "signal-radar-m365"   # MSAL token cache lives in the macOS keychain


MailAccount = Annotated[ImapAccount | GraphAccount, Field(discriminator="kind")]


class EmailMatch(_Strict):
    """Which messages in a folder belong to a source. All non-empty rules must pass.

    - from: substring of the sender address (any one)
    - from_name: sender display name, exact and case-insensitive (any one). Exact, because publishers
      that share one address name editions "TLDR", "TLDR AI", "TLDR Founders": a substring rule can't separate them.
    - subject_contains / subject_excludes: substrings of the subject
    """

    from_: list[str] = Field(default_factory=list, alias="from")
    from_name: list[str] = Field(default_factory=list)
    subject_contains: list[str] = Field(default_factory=list)
    subject_excludes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def matches(self, from_address: str, from_name: str, subject: str) -> bool:
        addr, name, subj = from_address.lower(), from_name.strip().lower(), subject.lower()
        if self.from_ and not any(f.lower() in addr for f in self.from_):
            return False
        if self.from_name and name not in {n.strip().lower() for n in self.from_name}:
            return False
        if self.subject_contains and not any(c.lower() in subj for c in self.subject_contains):
            return False
        return not any(x.lower() in subj for x in self.subject_excludes)


class _SourceBase(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)


class WebArchive(_Strict):
    """Public per-issue web pages, used to backfill history (ADR-0009). `{date}` is YYYY-MM-DD."""

    url: str
    parser: str
    weekdays_only: bool = True

    @field_validator("url")
    @classmethod
    def _has_date(cls, v: str) -> str:
        if "{date}" not in v:
            raise ValueError("archive url needs a {date} placeholder")
        return v


class EmailNewsletterSource(_SourceBase):
    type: Literal["email_newsletter"]
    account: str
    folder: str = "INBOX"
    match: EmailMatch
    parser: str = "generic"
    archive: WebArchive | None = None

    @field_validator("match")
    @classmethod
    def _match_not_empty(cls, v: EmailMatch) -> EmailMatch:
        if not v.from_:
            raise ValueError("email_newsletter needs at least one 'from' pattern")
        return v


class FeedSource(_SourceBase):
    """An RSS/Atom feed. Feeds keep only their newest entries, so ingest must run more often than they roll over."""

    type: Literal["rss"]
    url: HttpUrl
    edition: str | None = None             # display label; defaults to the feed's own title
    page_meta: bool = False                # title-only feeds: read og:description from the article's <head>
    strip: list[str] = Field(default_factory=list)  # regexes removed from summaries (CMS boilerplate)
    max_summary_chars: Annotated[int, Field(ge=100, le=2000)] = 700

    @field_validator("strip")
    @classmethod
    def _valid_regexes(cls, v: list[str]) -> list[str]:
        for p in v:
            try:
                re.compile(p)
            except re.error as e:
                raise ValueError(f"bad strip regex {p!r}: {e}") from None
        return v


class HackerNewsSource(_SourceBase):
    type: Literal["hn"]
    query: str
    min_points: int = 50


class ArxivSource(_SourceBase):
    type: Literal["arxiv"]
    query: str
    max_results: int = 50


class GithubSource(_SourceBase):
    type: Literal["github_releases"]
    repos: list[str]


Source = Annotated[
    EmailNewsletterSource | FeedSource | HackerNewsSource | ArxivSource | GithubSource,
    Field(discriminator="type"),
]


class SourcesConfig(_Strict):
    mail_accounts: dict[str, MailAccount] = Field(default_factory=dict)
    sources: list[Source] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_refs(self) -> SourcesConfig:
        ids = [s.id for s in self.sources]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate source ids: {sorted(dupes)}")
        for s in self.sources:
            if isinstance(s, EmailNewsletterSource) and s.account not in self.mail_accounts:
                raise ValueError(f"source {s.id!r} references unknown mail account {s.account!r}")
        return self

    def get(self, source_id: str) -> Source:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(source_id)


def load_sources(path: Path | None = None) -> SourcesConfig:
    path = path or CONFIG_DIR / "sources.yaml"
    return SourcesConfig.model_validate(yaml.safe_load(path.read_text()) or {})


def save_sources(cfg: SourcesConfig, path: Path | None = None) -> None:
    """Write sources.yaml back (used by subscribe/unsubscribe). Comments in the file are not preserved."""
    path = path or CONFIG_DIR / "sources.yaml"
    data = cfg.model_dump(mode="json", by_alias=True, exclude_defaults=False)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    tmp.replace(path)


# ---------------------------------------------------------------- graph.yaml


class Neo4jTarget(_Strict):
    uri: str = "bolt://localhost:7687"
    database: str
    username: str
    password_env: str

    @field_validator("database")
    @classmethod
    def _own_database(cls, v: str) -> str:
        if v.lower() in ("neo4j", "system", ""):
            raise ValueError(f"database {v!r} is a shared default; Signal Radar needs its own (ADR-0017)")
        return v


class GraphSync(_Strict):
    daily: bool = False
    batch_size: int = Field(500, ge=1, le=5000)
    timeout_seconds: int = Field(600, ge=10)
    max_node_delete_fraction: float = Field(0.2, ge=0, le=1)


class GraphEmbed(_Strict):
    """Vector search embeddings (ADR-0019): daily, after the sync, bounded so it can never run away."""

    daily: bool = False
    max_minutes: float = Field(10, gt=0, le=120)
    max_chunks: int | None = Field(5000, ge=1)


class GraphConfig(_Strict):
    """Where the Graqle graph is served from (ADR-0017). The file is always the source of truth."""

    connector: Literal["networkx", "neo4j"] = "networkx"
    file: str = "data/graqle/graqle.json"
    neo4j: Neo4jTarget | None = None
    sync: GraphSync = GraphSync()
    embed: GraphEmbed = GraphEmbed()

    @model_validator(mode="after")
    def _neo4j_needs_target(self) -> GraphConfig:
        if self.connector == "neo4j" and self.neo4j is None:
            raise ValueError("connector: neo4j needs a neo4j: section")
        return self

    @property
    def file_path(self) -> Path:
        return PROJECT_ROOT / self.file


def load_graph(path: Path | None = None) -> GraphConfig:
    path = path or CONFIG_DIR / "graph.yaml"
    return GraphConfig.model_validate(yaml.safe_load(path.read_text()) or {}) if path.exists() else GraphConfig()
