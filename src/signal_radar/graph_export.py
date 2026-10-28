"""Push the signal store into the project's Graqle graph (data/graqle/graqle.json).

Two Graqle commands, both run with data/graqle/ as working directory because Graqle writes manifests and audit
logs next to where it runs (keeps the repo root clean):
1. `graq scan docs <corpus>`: one DOCUMENT node per item; Graqle takes the node id from our frontmatter `id`.
2. `graq learn batch <file>`: entity nodes (id "ent:<key>", type = ontology type) with MENTIONS edges from item
   nodes, and the validated relations between entities.

`learn batch` only adds, so entities merged away since the last push would linger. The graph is rebuildable and a
full rebuild takes seconds, so a push with scan_docs starts from an empty graph. The graph file stays local
(never a shared backend).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .config import DATA_DIR, PROJECT_ROOT
from .corpus import CORPUS_DIR
from .extract import connect

GRAQLE_DIR = DATA_DIR / "graqle"
GRAPH_FILE = GRAQLE_DIR / "graqle.json"


def batch_payload(db: sqlite3.Connection, min_mentions: int = 2) -> dict:
    """Entities with at least `min_mentions` mentions, their MENTIONS edges and entity-entity relations."""
    ents = db.execute("SELECT id, key, name, type, first_seen, mentions FROM entities WHERE mentions >= ?",
                      (min_mentions,)).fetchall()
    keep = {e[0]: f"ent:{e[1]}" for e in ents}
    nodes = [{"id": f"ent:{k}", "type": t, "label": n,
              "description": f"{t} {n}: {m} mentions in Signal Radar's corpus, first seen {fs}."}
             for _, k, n, t, fs, m in ents]
    edges = [{"source": item, "target": keep[eid], "relation": "MENTIONS"}
             for eid, item in db.execute("SELECT entity_id, item_id FROM mentions") if eid in keep]
    rels = db.execute("SELECT src_id, relation, dst_id, COUNT(*) FROM relations GROUP BY 1, 2, 3").fetchall()
    edges += [{"source": keep[s], "target": keep[d], "relation": r} for s, r, d, _ in rels if s in keep and d in keep]
    return {"nodes": nodes, "edges": edges}


def _graq() -> str:
    exe = shutil.which("graq")
    if not exe:
        raise RuntimeError("graq CLI not found on PATH (pipx install graqle)")
    return exe


def push(db: sqlite3.Connection | None = None, min_mentions: int = 2, scan_docs: bool = True,
         corpus_dir: Path | None = None, graph_file: Path | None = None) -> dict:
    db = db if db is not None else connect()
    graph_file = graph_file or GRAPH_FILE
    graph_file.parent.mkdir(parents=True, exist_ok=True)
    cwd = graph_file.parent
    out: dict = {"graph": str(graph_file.relative_to(PROJECT_ROOT)) if graph_file.is_relative_to(PROJECT_ROOT) else str(graph_file)}
    if scan_docs:
        for stale in (graph_file, cwd / ".graqle-doc-manifest.json"):
            stale.unlink(missing_ok=True)
        r = subprocess.run([_graq(), "scan", "docs", str(corpus_dir or CORPUS_DIR), "-o", str(graph_file), "--no-link"],
                           cwd=cwd, capture_output=True, text=True, timeout=1800)
        out["scan_docs"] = {"returncode": r.returncode, "tail": r.stdout.strip().splitlines()[-12:]}
        if r.returncode != 0:
            raise RuntimeError(f"graq scan docs failed: {r.stderr[-500:]}")
    payload = batch_payload(db, min_mentions)
    batch_file = cwd / "signal_radar_batch.json"
    batch_file.write_text(json.dumps(payload))
    r = subprocess.run([_graq(), "learn", "batch", str(batch_file), "-g", str(graph_file)],
                       cwd=cwd, capture_output=True, text=True, timeout=1800)
    out["learn_batch"] = {"nodes": len(payload["nodes"]), "edges": len(payload["edges"]), "returncode": r.returncode,
                          "tail": r.stdout.strip().splitlines()[-6:]}
    if r.returncode != 0:
        raise RuntimeError(f"graq learn batch failed: {r.stderr[-500:]}")
    return out
