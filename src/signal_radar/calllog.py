"""Append-only log of every LLM call: model, tokens, cost, latency, input hash.

SQLite rather than JSONL because the main question asked of this log is a GROUP BY:
"for the same inputs, how did model A compare with model B on cost and output?"
`input_hash` excludes the model, so identical prompts sent to different models share a hash.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    run_id          TEXT NOT NULL,
    job             TEXT,
    role            TEXT NOT NULL,
    provider        TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    served_model    TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read_tokens INTEGER,
    cost_usd        REAL,          -- NULL when the model has no price entry
    latency_ms      INTEGER NOT NULL,
    stop_reason     TEXT,
    ok              INTEGER NOT NULL,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS ix_llm_calls_run ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS ix_llm_calls_hash ON llm_calls(input_hash);
"""


def input_hash(system: str | None, messages: list[dict[str, Any]], json_schema: dict | None) -> str:
    canonical = json.dumps(
        {"system": system, "messages": messages, "schema": json_schema},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass
class CallRecord:
    run_id: str
    job: str | None
    role: str
    provider: str
    requested_model: str
    served_model: str
    input_hash: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    stop_reason: str | None
    ok: bool
    error: str | None = None


class CallLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def month_spend(self, now=None) -> float:
        """USD spent on priced calls since the start of the current UTC month."""
        from datetime import UTC, datetime

        now = now or datetime.now(UTC)
        start = now.strftime("%Y-%m-01T00:00:00")
        with self._connect() as con:
            row = con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE ok = 1 AND ts >= ?",
                              (start,)).fetchone()
        return float(row[0] or 0)

    def write(self, rec: CallRecord) -> None:
        row = asdict(rec)
        row["ok"] = int(rec.ok)
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        with self._connect() as db:
            db.execute(f"INSERT INTO llm_calls ({cols}) VALUES ({marks})", row)

    def run_summary(self, run_id: str) -> list[dict[str, Any]]:
        q = """
        SELECT role, provider, served_model, COUNT(*) AS calls,
               SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
               SUM(cost_usd) AS cost_usd,
               SUM(CASE WHEN cost_usd IS NULL AND ok = 1 THEN 1 ELSE 0 END) AS unpriced_calls,
               SUM(1 - ok) AS failed_calls
        FROM llm_calls WHERE run_id = ? GROUP BY role, provider, served_model
        """
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(q, (run_id,))]

    def compare(self, input_hash_value: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Per-model averages over inputs that more than one model has seen."""
        q = """
        WITH shared AS (
            SELECT input_hash FROM llm_calls WHERE ok = 1
            GROUP BY input_hash HAVING COUNT(DISTINCT provider || '/' || served_model) > 1
        )
        SELECT provider, served_model, COUNT(*) AS calls,
               AVG(input_tokens) AS avg_in, AVG(output_tokens) AS avg_out,
               AVG(cost_usd) AS avg_cost_usd, AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls
        WHERE ok = 1 AND input_hash IN (SELECT input_hash FROM shared)
          AND (? IS NULL OR input_hash = ?)
        GROUP BY provider, served_model ORDER BY avg_cost_usd LIMIT ?
        """
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(q, (input_hash_value, input_hash_value, limit))]
