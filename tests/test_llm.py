import json
from pathlib import Path

import httpx
import pytest

from signal_radar.calllog import CallLog
from signal_radar.config import ProviderSettings, RoleSpec, load_models

FIXTURE_MODELS = Path(__file__).parent / "fixtures" / "models_v1.yaml"  # routing mechanics, not live model choices
from signal_radar.llm import (AnthropicClient, LLMError, LLMRefusal, LLMRouter, OllamaClient, OpenAIClient,
                              RawCompletion, RawEmbedding)


class FakeProvider:
    def __init__(self, text="ok", model=None, in_tok=1000, out_tok=500):
        self.text, self.model, self.in_tok, self.out_tok = text, model, in_tok, out_tok
        self.calls = []

    def complete(self, spec, system, messages, json_schema, params):
        self.calls.append(dict(spec=spec, system=system, messages=messages, schema=json_schema, params=params))
        return RawCompletion(self.text, self.model or spec.model, self.in_tok, self.out_tok, 0, "end_turn")

    def embed(self, spec, texts, params):
        return RawEmbedding([[0.1, 0.2]] * len(texts), spec.model, 7)


def make_router(tmp_path, provider, overrides=None):
    router = LLMRouter(cfg=load_models(FIXTURE_MODELS, overrides=overrides), log=CallLog(tmp_path / "calls.sqlite"), job="test")
    for name in ("anthropic", "ollama", "openai"):
        router._clients[name] = provider
    return router


def test_role_routing_params_and_cost(tmp_path):
    fake = FakeProvider()
    r = make_router(tmp_path, fake)
    res = r.complete("reasoning", "hi", temperature=None)
    call = fake.calls[0]
    assert call["spec"].model == "claude-opus-5-5"
    assert call["params"]["effort"] == "high" and "temperature" in call["params"]
    assert res.cost_usd == pytest.approx((1000 * 4 + 500 * 20) / 1e6)


def test_cli_override_changes_model_without_code_change(tmp_path):
    fake = FakeProvider()
    r = make_router(tmp_path, fake, overrides=["reasoning=ollama/llama3.1:8b"])
    res = r.complete("reasoning", "hi")
    assert fake.calls[0]["spec"].key == "ollama/llama3.1:8b"
    assert res.cost_usd == 0


def test_fallback_model_is_priced_as_served(tmp_path):
    r = make_router(tmp_path, FakeProvider(model="claude-opus-4-8"))
    res = r.complete("reasoning", "hi")
    assert res.model == "claude-opus-4-8"
    assert res.cost_usd == pytest.approx((1000 * 5 + 500 * 25) / 1e6)


def test_unknown_price_is_null_not_guessed(tmp_path):
    r = make_router(tmp_path, FakeProvider(), overrides=["writing=openai/some-new-model"])
    assert r.complete("writing", "hi").cost_usd is None
    summary = r.log.run_summary(r.run_id)
    assert summary[0]["unpriced_calls"] == 1


def test_json_schema_parsed_and_bad_json_raises(tmp_path):
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    r = make_router(tmp_path, FakeProvider(text='{"a": 1}'))
    assert r.complete("extraction", "x", json_schema=schema).data == {"a": 1}
    r = make_router(tmp_path, FakeProvider(text="not json"))
    with pytest.raises(LLMError):
        r.complete("extraction", "x", json_schema=schema)


def test_same_input_two_models_is_comparable(tmp_path):
    fake = FakeProvider()
    r1 = make_router(tmp_path, fake)
    r1.complete("extraction", "same prompt")
    r2 = make_router(tmp_path, fake, overrides=["extraction=ollama/other:7b"])
    r2.complete("extraction", "same prompt")
    rows = r1.log.compare()
    assert {row["served_model"] for row in rows} == {"qwen3:8b", "other:7b"}


def test_failures_are_logged(tmp_path):
    class Boom(FakeProvider):
        def complete(self, *a):
            raise RuntimeError("down")
    r = make_router(tmp_path, Boom())
    with pytest.raises(RuntimeError):
        r.complete("writing", "x")
    assert r.log.run_summary(r.run_id)[0]["failed_calls"] == 1


def test_embed_routes_to_embedding_role(tmp_path):
    r = make_router(tmp_path, FakeProvider())
    res = r.embed(["a", "b"])
    assert len(res.vectors) == 2 and res.model == "nomic-embed-text"
    with pytest.raises(LLMError):
        r.complete("embedding", "x")


def test_ollama_request_shape():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"model": "qwen3:8b", "message": {"content": "{}"},
                                         "prompt_eval_count": 12, "eval_count": 3, "done_reason": "stop"})

    c = OllamaClient(ProviderSettings(default_host="http://ollama.test"))
    c.http = httpx.Client(base_url="http://ollama.test", transport=httpx.MockTransport(handler))
    raw = c.complete(RoleSpec(provider="ollama", model="qwen3:8b"), "sys", [{"role": "user", "content": "u"}],
                     {"type": "object"}, {"temperature": 0, "think": False, "max_tokens": 50})
    assert seen["think"] is False and seen["format"] == {"type": "object"}
    assert seen["options"] == {"temperature": 0, "num_predict": 50}
    assert seen["messages"][0]["role"] == "system"
    assert (raw.input_tokens, raw.output_tokens) == (12, 3)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _anthropic_with(resp):
    captured = {}

    def create(**kw):
        captured.update(kw)
        return resp

    c = object.__new__(AnthropicClient)
    c.client = _Obj(messages=_Obj(create=create), beta=_Obj(messages=_Obj(create=create)))
    return c, captured


def _resp(stop="end_turn", model="claude-opus-5-5"):
    usage = _Obj(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=90)
    blocks = [_Obj(type="thinking", thinking=""), _Obj(type="text", text="hello")]
    return _Obj(stop_reason=stop, model=model, content=blocks, usage=usage, stop_details=_Obj(category="cyber"))


def test_anthropic_param_mapping():
    c, kw = _anthropic_with(_resp())
    raw = c.complete(RoleSpec(provider="anthropic", model="claude-opus-5-5"), "sys",
                     [{"role": "user", "content": "u"}], {"type": "object"},
                     {"max_tokens": 1000, "effort": "high", "fallbacks": "default"})
    assert kw["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["system"] == "sys" and kw["max_tokens"] == 1000
    assert raw.text == "hello" and raw.input_tokens == 100 and raw.cache_read_tokens == 90


def test_anthropic_refusal_raises():
    c, _ = _anthropic_with(_resp(stop="refusal"))
    with pytest.raises(LLMRefusal, match="cyber"):
        c.complete(RoleSpec(provider="anthropic", model="m"), None, [], None, {})


def test_openai_compatible_localhost_needs_no_key_but_remote_does(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.setattr("signal_radar.llm._keychain", lambda account: None)
    monkeypatch.delenv("LOCAL_KEY", raising=False)
    local = OpenAIClient(ProviderSettings(kind="openai", base_url="http://localhost:1234/v1", api_key_env="LOCAL_KEY"))
    assert local.client.api_key == "local"
    with pytest.raises(LLMError, match="no API key"):
        OpenAIClient(ProviderSettings(kind="openai", base_url="https://openrouter.ai/api/v1", api_key_env="LOCAL_KEY"))
