"""Local console server: serves the built console edition and the two live routes it needs.

Binds to 127.0.0.1 only. The routes are deliberately small:
- POST /api/feedback {item_id, vote: -1|0|1} stores a vote (survives rebuilds; the next build hides "-1" items)
- GET  /api/votes                             current votes, so the static page shows them without a rebuild
- POST /api/ask {question}                    grounded answer from the corpus (see prose.answer)
Everything else is the static folder, identical to what a public host would serve.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..extract import Taxonomy, connect
from .build import SITE_DIR
from .prose import answer


class Vote(BaseModel):
    item_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    vote: int = Field(ge=-1, le=1)


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)


def create_app(site_dir: Path | None = None, db_path: Path | None = None, complete=None) -> FastAPI:
    site_dir = site_dir or SITE_DIR
    if not (site_dir / "index.html").exists():
        raise RuntimeError(f"no site in {site_dir}; run `signal-radar site build` first")
    app = FastAPI(title="Signal Radar console", docs_url=None, redoc_url=None)

    def db():
        return connect(db_path) if db_path else connect()

    @app.post("/api/feedback")
    def feedback(v: Vote) -> dict:
        con = db()
        if con.execute("SELECT 1 FROM items WHERE id = ?", (v.item_id,)).fetchone() is None:
            raise HTTPException(404, "unknown item")
        if v.vote == 0:
            con.execute("DELETE FROM feedback WHERE item_id = ?", (v.item_id,))
        else:
            con.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?)",
                        (v.item_id, v.vote, datetime.now(UTC).isoformat()))
        con.commit()
        return {"ok": True}

    @app.get("/api/votes")
    def votes() -> dict:
        return dict(db().execute("SELECT item_id, vote FROM feedback").fetchall())

    @app.post("/api/ask")
    def ask(q: Question) -> dict:  # sync: FastAPI runs it in a worker thread; the model call may take a while
        return answer(db(), q.question, Taxonomy.load().lens, complete=complete)

    app.mount("/", StaticFiles(directory=site_dir, html=True), name="site")
    return app
