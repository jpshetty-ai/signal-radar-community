"""Candidate roles, fallback, budget and strategy (ADR-0013)."""

from datetime import UTC, datetime

import pytest
import yaml

from signal_radar.calllog import CallLog, CallRecord
from signal_radar.config import ModelsConfig, load_models
from signal_radar.llm import LLMError, LLMRouter, RawCompletion

CFG = {
    "roles": {
        "extraction": {"provider": "ollama", "model": "qwen3:8b"},
        "embedding": {"provider": "ollama", "model": "nomic-embed-text"},
        "writing": {"candidates": ["pricey", "cheap", "local"], "strategy": "cheapest_passing", "min_quality": 0.8},
        "reasoning": {"candidates": ["cheap", "local"]},
    },
    "providers": {"ollama": {}, "hub": {"kind": "openai", "base_url": "https://hub.example/v1", "api_key_env": "HUB_KEY"}},
    "catalog": {
        "pricey": {"provider": "hub", "model": "big/model", "price": {"input": 4, "output": 20}},
        "cheap": {"provider": "hub", "model": "small/model", "price": {"input": 0.1, "output": 0.5}},
        "local": {"provider": "ollama", "model": "qwen3:8b", "price": {"input": 0, "output": 0}},
    },
    "budget": {"monthly_usd": 1.0},
}


class Fake:
    def __init__(self, fail=(), text="ok"):
        self.fail, self.text, self.calls = set(fail), text, []

    def complete(self, spec, system, messages, json_schema, params):
        self.calls.append(spec.model)
        if spec.model in self.fail:
            raise RuntimeError(f"{spec.model} is down")
        return RawCompletion(self.text, spec.model, 1000, 100)


def router(tmp_path, fake, scores=None, cfg=CFG):
    r = LLMRouter(cfg=ModelsConfig.model_validate(cfg), log=CallLog(tmp_path / "calls.sqlite"), job="t", scores=scores)
    r._clients = {"hub": fake, "ollama": fake}
    return r


def test_config_validation_catches_unknown_candidates_and_kindless_providers():
    bad = {**CFG, "roles": {**CFG["roles"], "writing": {"candidates": ["nope"]}}}
    with pytest.raises(ValueError, match="not in the catalog"):
        ModelsConfig.model_validate(bad)
    with pytest.raises(ValueError, match="needs a kind"):
        ModelsConfig.model_validate({**CFG, "providers": {"mystery": {}}})
    with pytest.raises(ValueError, match="either provider"):
        ModelsConfig.model_validate({**CFG, "roles": {**CFG["roles"], "writing": {"provider": "hub", "model": "x",
                                                                                  "candidates": ["cheap"]}}})


def test_falls_through_candidates_and_records_which_served(tmp_path):
    fake = Fake(fail={"small/model"})
    res = router(tmp_path, fake).complete("reasoning", "hi")
    assert fake.calls == ["small/model", "qwen3:8b"] and res.candidate == "local" and res.cost_usd == 0
    with pytest.raises(LLMError, match="no candidate succeeded"):
        router(tmp_path, Fake(fail={"small/model", "qwen3:8b"})).complete("reasoning", "hi")


def test_cheapest_passing_uses_eval_scores_then_listed_order(tmp_path):
    cfg = ModelsConfig.model_validate(CFG)
    assert [c for c, _ in cfg.resolve("writing")] == ["pricey", "cheap", "local"]  # no scores yet: listed order
    scores = {"writing": {"pricey": {"quality": 0.95, "cost_per_task": 0.02},
                          "cheap": {"quality": 0.9, "cost_per_task": 0.001},
                          "local": {"quality": 0.6, "cost_per_task": 0}}}
    assert [c for c, _ in cfg.resolve("writing", scores)] == ["cheap", "pricey", "local"]  # local fails the bar
    scores["writing"]["cheap"]["quality"] = 0.7
    assert [c for c, _ in cfg.resolve("writing", scores)] == ["pricey", "cheap", "local"]


def test_budget_skips_paid_candidates_once_the_month_is_spent(tmp_path):
    fake = Fake()
    r = router(tmp_path, fake)
    r.log.write(CallRecord(run_id="x", job="t", role="writing", provider="hub", requested_model="big/model",
                           served_model="big/model", input_hash="h", input_tokens=1, output_tokens=1,
                           cache_read_tokens=0, cost_usd=1.5, latency_ms=1, stop_reason="stop", ok=True, error=None))
    assert r.log.month_spend(datetime.now(UTC)) == pytest.approx(1.5)
    res = r.complete("reasoning", "hi")
    assert fake.calls == ["qwen3:8b"] and res.candidate == "local"  # cheap is paid -> skipped; local is free


def test_missing_key_is_logged_once_per_run(tmp_path):
    r = LLMRouter(cfg=ModelsConfig.model_validate(CFG), log=CallLog(tmp_path / "c.sqlite"), job="t")
    r._clients = {"ollama": Fake()}
    for _ in range(3):
        assert r.complete("reasoning", "hi").candidate == "local"
    failures = r.log._connect().execute("SELECT COUNT(*) FROM llm_calls WHERE ok = 0").fetchone()[0]
    assert failures == 1 and "HUB_KEY" in r._unavailable["hub"]


def test_catalog_override_from_cli(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(CFG))
    cfg = load_models(p, overrides=["writing=@local"])
    assert [c for c, _ in cfg.resolve("writing")] == ["local"]
    assert cfg.price_for("hub", "small/model").output == 0.5  # catalog prices are used for cost
