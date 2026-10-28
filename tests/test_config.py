import pytest
import yaml
from pydantic import ValidationError

from signal_radar.config import (
    CONFIG_DIR, EmailNewsletterSource, FeedSource, SourcesConfig, load_models, load_sources,
    parse_override, save_sources,
)


def test_repo_models_yaml_is_valid():
    cfg = load_models()
    assert set(cfg.roles) == {"extraction", "reasoning", "writing", "embedding"}


def test_repo_sources_yaml_is_valid():
    cfg = load_sources()
    assert isinstance(cfg.get("tldr_ai"), EmailNewsletterSource)


def test_override_splits_on_first_slash_only():
    role, spec = parse_override("extraction=openai/meta-llama/Llama-3.3-70B")
    assert (role, spec.provider, spec.model) == ("extraction", "openai", "meta-llama/Llama-3.3-70B")


@pytest.mark.parametrize("bad", ["extraction", "extraction=ollama", "nope=ollama/x", "extraction=bogus/x",
                                 "writing=@not-in-catalog"])
def test_override_rejects_bad_input(bad):
    with pytest.raises((ValueError, ValidationError)):
        load_models(overrides=[bad])  # provider names are config-defined, so validation needs the config


def test_override_keeps_params_only_for_same_provider():
    cfg = load_models(overrides=["extraction=ollama/llama3.1:8b", "reasoning=openai/gpt-x"])
    assert cfg.roles["extraction"].model == "llama3.1:8b"
    assert cfg.roles["extraction"].params.get("temperature") == 0
    assert cfg.roles["reasoning"].params == {}


def test_pricing_exact_then_wildcard_then_unknown():
    cfg = load_models()
    assert cfg.price_for("anthropic", "claude-opus-5-5").input == 4.0
    assert cfg.price_for("ollama", "anything").output == 0
    assert cfg.price_for("openai", "gpt-unknown") is None


def test_sources_reject_unknown_account_and_dupes():
    src = {"id": "x", "type": "email_newsletter", "account": "nope", "match": {"from": ["a"]}}
    with pytest.raises(ValidationError, match="unknown mail account"):
        SourcesConfig.model_validate({"sources": [src]})
    feed = {"id": "f", "type": "rss", "url": "https://example.com/feed"}
    with pytest.raises(ValidationError, match="duplicate"):
        SourcesConfig.model_validate({"sources": [feed, feed]})


def test_save_sources_round_trips(tmp_path):
    cfg = load_sources()
    cfg.sources.append(FeedSource(id="new_feed", type="rss", url="https://example.com/rss"))
    out = tmp_path / "sources.yaml"
    save_sources(cfg, out)
    again = load_sources(out)
    assert [s.id for s in again.sources] == [s.id for s in cfg.sources]
    assert yaml.safe_load(out.read_text())["sources"][0]["match"]["from"]  # alias written as 'from'
