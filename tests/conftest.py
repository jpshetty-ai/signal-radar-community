import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_neo4j(monkeypatch):
    """Tests never reach the live Neo4j (ADR-0017/0018): the brief sees "graph unavailable" unless a test injects
    a fake reader."""
    from signal_radar import graph_signals
    from signal_radar.graph_sync import SyncError

    def blocked():
        raise SyncError("no live Neo4j in tests")

    monkeypatch.setattr(graph_signals, "neo4j_reader", blocked)


@pytest.fixture(autouse=True)
def _no_live_vectors(monkeypatch):
    """Tests never load the embedding model or read the real vector cache (ADR-0019): hybrid retrieval runs
    keyword-only unless a test injects a ranking."""
    from signal_radar import vectors

    monkeypatch.setattr(vectors, "item_ranking", lambda question, k=30: [])
