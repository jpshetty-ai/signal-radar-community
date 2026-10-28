import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sync", ROOT / "scripts" / "sync_graqle_config.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


V1 = ROOT / "tests" / "fixtures" / "models_v1.yaml"


def test_reasoning_role_drives_graqle_model():
    doc, warnings = sync.build(V1)
    assert doc["model"] == {"backend": "anthropic", "model": "claude-opus-5-5", "api_key": "${ANTHROPIC_API_KEY}"}
    assert doc["graph"]["path"].startswith("data/graqle/")
    assert any("embedding role" in w for w in warnings)


def test_candidate_roles_map_to_first_choice_by_provider_kind():
    doc, _ = sync.build()  # live config: reasoning candidates served via OpenRouter (an OpenAI-compatible kind)
    assert doc["model"]["backend"] == "openai" and doc["model"]["endpoint"] == "https://openrouter.ai/api/v1"
    assert doc["model"]["api_key"] == "${OPENROUTER_API_KEY}"


def test_committed_graqle_yaml_is_in_sync():
    doc, _ = sync.build()
    assert (ROOT / "graqle.yaml").read_text() == sync.render(doc), "run scripts/sync_graqle_config.py"


def test_generated_yaml_validates_against_graqle_schema():
    pytest.importorskip("graqle")
    from graqle.config.settings import GraqleConfig

    for path in (V1, None):
        doc, _ = sync.build(path)
        cfg = GraqleConfig.model_validate(yaml.safe_load(sync.render(doc)))
        assert cfg.models["extraction"].backend == "ollama"


def test_neo4j_connector_keeps_the_file_as_fallback_and_the_password_as_env_ref(tmp_path):
    """ADR-0017: graq reads Neo4j, falls back to the file, and graqle.yaml never holds a password."""
    pytest.importorskip("graqle")
    from graqle.config.settings import GraqleConfig

    g = tmp_path / "graph.yaml"
    g.write_text(yaml.safe_dump({"connector": "neo4j", "file": "data/graqle/graqle.json",
                                 "neo4j": {"uri": "bolt://localhost:7687", "database": "signalradar",
                                           "username": "signal_radar", "password_env": "SIGNAL_RADAR_NEO4J_PASSWORD"}}))
    doc, _ = sync.build(graph_path=g)
    assert doc["graph"] == {"connector": "neo4j", "path": "data/graqle/graqle.json", "uri": "bolt://localhost:7687",
                            "username": "signal_radar", "password": "${SIGNAL_RADAR_NEO4J_PASSWORD}",
                            "database": "signalradar"}
    with pytest.warns(DeprecationWarning):   # graq 0.85: graph.connector is the runtime switch, though deprecated
        GraqleConfig.model_validate(yaml.safe_load(sync.render(doc)))


def test_networkx_connector_reads_the_file_only(tmp_path):
    g = tmp_path / "graph.yaml"
    g.write_text("connector: networkx\nfile: data/graqle/graqle.json\n")
    assert sync.build(graph_path=g)[0]["graph"] == {"connector": "networkx", "path": "data/graqle/graqle.json"}
