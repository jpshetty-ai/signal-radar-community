"""The only module that talks to LLM providers.

Callers ask for a *role* (extraction, reasoning, writing, embedding), never a model:

    router = LLMRouter.from_config(job="weekly_brief")
    result = router.complete("reasoning", "Summarise ...", json_schema=SCHEMA)

Which provider and model serve a role is decided by config/models.yaml plus any
`--model role=provider/model` overrides. Every call is written to the call log with
tokens, cost and an input hash so models can be compared on identical inputs.

Provider SDKs are imported lazily: a setup that only uses Ollama never needs the
anthropic or openai packages installed.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .calllog import CallLog, CallRecord, input_hash
from .config import DATA_DIR, ModelsConfig, ProviderSettings, Role, RoleSpec, load_models

Messages = list[dict[str, Any]]


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    """The provider declined the request (Anthropic stop_reason == 'refusal')."""


@dataclass
class RawCompletion:
    text: str
    served_model: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None = None
    stop_reason: str | None = None


@dataclass
class RawEmbedding:
    vectors: list[list[float]]
    served_model: str
    input_tokens: int | None


@dataclass
class LLMResult:
    text: str
    role: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    stop_reason: str | None
    input_hash: str
    data: Any = None  # parsed JSON when a json_schema was requested
    candidate: str | None = None  # catalog id (or provider/model) that served the call


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    provider: str
    model: str
    input_tokens: int | None
    cost_usd: float | None
    latency_ms: int


class ProviderClient(Protocol):
    def complete(
        self,
        spec: RoleSpec,
        system: str | None,
        messages: Messages,
        json_schema: dict | None,
        params: dict[str, Any],
    ) -> RawCompletion: ...

    def embed(self, spec: RoleSpec, texts: list[str], params: dict[str, Any]) -> RawEmbedding: ...


# ------------------------------------------------------------------ providers


class OllamaClient:
    """Native Ollama HTTP API (/api/chat, /api/embed). No SDK needed."""

    # Keys Ollama takes at the top level of the request; everything else goes in `options`.
    TOP_LEVEL = {"think", "keep_alive"}

    def __init__(self, settings: ProviderSettings):
        host = os.environ.get(settings.host_env or "", "") or settings.default_host or "http://localhost:11434"
        self.host = host.rstrip("/")
        self.http = httpx.Client(base_url=self.host, timeout=settings.timeout_s)

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = self.http.post(path, json=body)
        except httpx.ConnectError as e:
            raise LLMError(f"Ollama not reachable at {self.host}; is `ollama serve` running?") from e
        if r.status_code >= 400:
            raise LLMError(f"ollama {r.status_code}: {r.text[:300]}")
        return r.json()

    def _split(self, params: dict[str, Any]) -> tuple[dict, dict]:
        top = {k: v for k, v in params.items() if k in self.TOP_LEVEL}
        options = {k: v for k, v in params.items() if k not in self.TOP_LEVEL}
        return top, options

    def complete(self, spec, system, messages, json_schema, params):
        top, options = self._split(params)
        if "max_tokens" in options:  # accept the cross-provider name; Ollama calls it num_predict
            options.setdefault("num_predict", options.pop("max_tokens"))
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        body: dict[str, Any] = {"model": spec.model, "messages": msgs, "stream": False, "options": options, **top}
        if json_schema:
            body["format"] = json_schema
        d = self._post("/api/chat", body)
        return RawCompletion(
            text=d["message"]["content"],
            served_model=d.get("model", spec.model),
            input_tokens=d.get("prompt_eval_count"),
            output_tokens=d.get("eval_count"),
            stop_reason=d.get("done_reason"),
        )

    def embed(self, spec, texts, params):
        params = dict(params)
        prefix, kind = params.pop("query_prefix", ""), params.pop("kind", None)
        params.pop("batch_size", None)
        if kind == "query":
            texts = [prefix + t for t in texts]
        d = self._post("/api/embed", {"model": spec.model, "input": texts, **params})
        return RawEmbedding(d["embeddings"], d.get("model", spec.model), d.get("prompt_eval_count"))


class AnthropicClient:
    FALLBACK_BETA = "server-side-fallback-2026-07-01"  # gates fallbacks="default"

    def __init__(self, settings: ProviderSettings):
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("provider 'anthropic' needs: pip install 'signal-radar[anthropic]'") from e
        key = os.environ.get(settings.api_key_env or "ANTHROPIC_API_KEY") or None
        # api_key=None lets the SDK resolve ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
        self.client = anthropic.Anthropic(api_key=key, timeout=settings.timeout_s)

    def complete(self, spec, system, messages, json_schema, params):
        p = dict(params)
        kwargs: dict[str, Any] = {
            "model": spec.model,
            "max_tokens": p.pop("max_tokens", 16000),
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        output_config: dict[str, Any] = {}
        if effort := p.pop("effort", None):
            output_config["effort"] = effort
        if json_schema:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            kwargs["output_config"] = output_config
        fallbacks = p.pop("fallbacks", None)
        kwargs.update(p)

        try:
            if fallbacks:
                resp = self.client.beta.messages.create(betas=[self.FALLBACK_BETA], fallbacks=fallbacks, **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except TypeError as e:
            # The SDK raises TypeError when no credential resolves (no key, token or `ant auth login` profile).
            if "authentication" in str(e).lower():
                raise LLMError("anthropic: no API key (set ANTHROPIC_API_KEY in .env)") from e
            raise

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal(f"{resp.model} refused: {getattr(details, 'category', None)}")
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        return RawCompletion(
            text=text,
            served_model=resp.model,  # may differ from spec.model after a server-side fallback
            input_tokens=u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0),
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            stop_reason=resp.stop_reason,
        )

    def embed(self, spec, texts, params):
        raise LLMError("Anthropic has no embeddings endpoint; assign the embedding role to ollama or openai")


class OpenAIClient:
    """OpenAI or any OpenAI-compatible server (LM Studio, vLLM, OpenRouter) via base_url."""

    def __init__(self, settings: ProviderSettings):
        try:
            import openai
        except ImportError as e:
            raise LLMError("provider 'openai' needs: pip install 'signal-radar[openai]'") from e
        key = os.environ.get(settings.api_key_env or "OPENAI_API_KEY") or _keychain(settings.api_key_env)
        if not key:
            raise LLMError(f"no API key: set {settings.api_key_env or 'OPENAI_API_KEY'} in .env or the macOS keychain "
                           f"(security add-generic-password -s signal-radar -a {settings.api_key_env} -w)")
        self.client = openai.OpenAI(api_key=key, base_url=settings.base_url, timeout=settings.timeout_s,
                                    default_headers=settings.headers or None)
        self.extra_body = settings.extra_body

    def complete(self, spec, system, messages, json_schema, params):
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        kwargs: dict[str, Any] = {"model": spec.model, "messages": msgs, **params}
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        if json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema, "strict": True},
            }
        resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        u = resp.usage
        return RawCompletion(
            text=choice.message.content or "",
            served_model=resp.model,
            input_tokens=u.prompt_tokens if u else None,
            output_tokens=u.completion_tokens if u else None,
            stop_reason=choice.finish_reason,
        )

    def embed(self, spec, texts, params):
        resp = self.client.embeddings.create(model=spec.model, input=texts, **params)
        return RawEmbedding([d.embedding for d in resp.data], resp.model, resp.usage.prompt_tokens)


class FastEmbedClient:
    """Embedding models run in-process with fastembed (ONNX Runtime): no server, no PyTorch, memory only while
    embedding. Used for vector search (ADR-0019). Embeddings only: it has no chat models.

    Retrieval models are often asymmetric: questions carry an instruction prefix, stored passages do not. The
    prefix is a model property, so it lives in models.yaml (`params.query_prefix`) and is applied only when the
    caller embeds a question (`kind="query"`).
    """

    _models: dict[str, Any] = {}  # loaded once per process; a model load takes about a second

    def __init__(self, settings: ProviderSettings):
        try:
            import fastembed  # noqa: F401
        except ImportError as e:
            raise LLMError("provider 'fastembed' needs: pip install fastembed") from e
        self.settings = settings

    def complete(self, spec, system, messages, json_schema, params):
        raise LLMError("fastembed serves embedding models only; assign it to the embedding role")

    def _model(self, name: str, threads: int | None):
        if name not in self._models:
            from fastembed import TextEmbedding

            # Not fastembed's default (the system temp dir, which macOS clears): the daily job must not re-download.
            self._models[name] = TextEmbedding(model_name=name, threads=threads, cache_dir=str(DATA_DIR / "models"))
        return self._models[name]

    def embed(self, spec, texts, params):
        model = self._model(spec.model, params.get("threads"))
        if params.get("kind") == "query":
            texts = [params.get("query_prefix", "") + t for t in texts]
        vectors = [v.tolist() for v in model.embed(texts, batch_size=int(params.get("batch_size", 64)))]
        return RawEmbedding(vectors, spec.model, None)


PROVIDERS: dict[str, type] = {"ollama": OllamaClient, "anthropic": AnthropicClient, "openai": OpenAIClient,
                              "fastembed": FastEmbedClient}


def _keychain(account: str | None) -> str | None:
    """API keys may live in the macOS keychain (service 'signal-radar', account = the env var name)."""
    if not account:
        return None
    try:
        import keyring

        return keyring.get_password("signal-radar", account)
    except Exception:  # noqa: BLE001 - no keyring backend: the env var is the only source
        return None


# --------------------------------------------------------------------- router


def _strip_fences(text: str) -> str:
    """Some OpenAI-compatible models wrap JSON in ```json fences even in structured-output mode."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _cost(cfg: ModelsConfig, provider: str, model: str, raw_in: int | None, out: int | None, cache_read: int | None):
    price = cfg.price_for(provider, model)
    if price is None or raw_in is None or out is None:
        return None
    cached = cache_read or 0
    cache_rate = price.cache_read if price.cache_read is not None else price.input
    return round(((raw_in - cached) * price.input + cached * cache_rate + out * price.output) / 1_000_000, 6)


@dataclass
class LLMRouter:
    cfg: ModelsConfig
    log: CallLog | None = None
    job: str | None = None
    run_id: str = field(default_factory=lambda: time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6])
    scores: dict | None = None  # data/model_eval.json, for the cheapest_passing strategy
    _clients: dict[str, ProviderClient] = field(default_factory=dict)
    _unavailable: dict[str, str] = field(default_factory=dict)  # provider -> why (e.g. no key), for this run

    @classmethod
    def from_config(cls, overrides: list[str] | None = None, job: str | None = None, log: bool = True) -> LLMRouter:
        cfg = load_models(overrides=overrides)
        scores_path = DATA_DIR / "model_eval.json"
        scores = json.loads(scores_path.read_text()).get("scores") if scores_path.exists() else None
        return cls(cfg=cfg, job=job, log=CallLog(DATA_DIR / "llm_calls.sqlite") if log else None, scores=scores)

    def candidates(self, role: Role) -> list[tuple[str, RoleSpec]]:
        return self.cfg.resolve(role, self.scores)

    def spec(self, role: Role) -> RoleSpec:
        return self.candidates(role)[0][1]

    def _client(self, provider: str) -> ProviderClient:
        if provider in self._unavailable:
            raise LLMError(self._unavailable[provider])
        if provider not in self._clients:
            settings = self.cfg.providers.get(provider) or ProviderSettings()
            kind = self.cfg.kind_of(provider)
            if kind not in PROVIDERS:
                raise LLMError(f"provider {provider!r} has unknown kind {kind!r}")
            try:
                self._clients[provider] = PROVIDERS[kind](settings)
            except LLMError as e:  # missing key or SDK: skip this provider for the rest of the run, quietly
                self._unavailable[provider] = str(e)
                raise
        return self._clients[provider]

    def _over_budget(self, spec: RoleSpec) -> bool:
        cap = self.cfg.budget.monthly_usd
        price = self.cfg.price_for(spec.provider, spec.model)
        if cap is None or self.log is None or price is None or (price.input == 0 and price.output == 0):
            return False
        return self.log.month_spend() >= cap

    def complete(
        self,
        role: Role,
        prompt: str | Messages,
        *,
        system: str | None = None,
        json_schema: dict | None = None,
        **params: Any,
    ) -> LLMResult:
        """Run one completion for `role`. Per-call params override the role's params from models.yaml.

        With json_schema, the provider is asked for schema-constrained output and the parsed
        object is returned in `.data`. A response that does not parse raises LLMError.
        """
        if role == "embedding":
            raise LLMError("use embed() for the embedding role")
        messages: Messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        h = input_hash(system, messages, json_schema)
        errors: list[str] = []
        for label, spec in self.candidates(role):
            if self._over_budget(spec):
                errors.append(f"{label}: monthly budget of ${self.cfg.budget.monthly_usd} reached")
                continue
            try:
                return self._complete_one(role, label, spec, system, messages, json_schema, params, h)
            except LLMRefusal:
                raise
            except Exception as e:  # noqa: BLE001 - try the next candidate; the failure is logged
                errors.append(f"{label}: {str(e)[:160]}")
        raise LLMError(f"role {role}: no candidate succeeded ({'; '.join(errors)})")

    def _complete_one(self, role, label, spec, system, messages, json_schema, params, h) -> LLMResult:
        merged = {**spec.params, **params}
        t0 = time.monotonic()
        known_down = spec.provider in self._unavailable
        try:
            raw = self._client(spec.provider).complete(spec, system, messages, json_schema, merged)
        except Exception as e:
            if not known_down:  # log a missing key once per run, not once per call
                self._log(role, spec, spec.model, h, None, None, None, None, t0, None, ok=False, error=repr(e)[:500])
            raise
        cost = _cost(self.cfg, spec.provider, raw.served_model, raw.input_tokens, raw.output_tokens, raw.cache_read_tokens)
        if cost is None:  # OpenRouter reports e.g. "z-ai/glm-5.3-20260818"; price by the requested model then
            cost = _cost(self.cfg, spec.provider, spec.model, raw.input_tokens, raw.output_tokens, raw.cache_read_tokens)
        latency = self._log(
            role, spec, raw.served_model, h, raw.input_tokens, raw.output_tokens, raw.cache_read_tokens,
            cost, t0, raw.stop_reason, ok=True,
        )
        data = None
        if json_schema is not None:
            try:
                data = json.loads(_strip_fences(raw.text))
            except json.JSONDecodeError as e:
                raise LLMError(f"{spec.key} returned non-JSON for a json_schema request: {raw.text[:200]!r}") from e
        return LLMResult(
            text=raw.text, role=role, provider=spec.provider, model=raw.served_model,
            input_tokens=raw.input_tokens, output_tokens=raw.output_tokens, cost_usd=cost,
            latency_ms=latency, stop_reason=raw.stop_reason, input_hash=h, data=data, candidate=label,
        )

    def embed(self, texts: list[str], **params: Any) -> EmbedResult:
        spec = self.spec("embedding")
        h = input_hash(None, [{"embed": texts}], None)
        t0 = time.monotonic()
        try:
            raw = self._client(spec.provider).embed(spec, texts, {**spec.params, **params})
        except Exception as e:
            self._log("embedding", spec, spec.model, h, None, None, None, None, t0, None, ok=False, error=repr(e)[:500])
            raise
        cost = _cost(self.cfg, spec.provider, raw.served_model, raw.input_tokens, 0, 0)
        latency = self._log("embedding", spec, raw.served_model, h, raw.input_tokens, 0, 0, cost, t0, None, ok=True)
        return EmbedResult(raw.vectors, spec.provider, raw.served_model, raw.input_tokens, cost, latency)

    def _log(self, role, spec, served, h, tin, tout, tcache, cost, t0, stop, *, ok, error=None) -> int:
        latency = int((time.monotonic() - t0) * 1000)
        if self.log:
            self.log.write(CallRecord(
                run_id=self.run_id, job=self.job, role=role, provider=spec.provider,
                requested_model=spec.model, served_model=served, input_hash=h,
                input_tokens=tin, output_tokens=tout, cache_read_tokens=tcache, cost_usd=cost,
                latency_ms=latency, stop_reason=stop, ok=ok, error=error,
            ))
        return latency
