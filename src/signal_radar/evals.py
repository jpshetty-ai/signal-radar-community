"""Model evaluation on Signal Radar's own tasks (ADR-0013): which candidate is good enough, at what cost.

Benchmarks rank general intelligence; our tasks are narrower: summarise a theme faithfully from 10 items, answer
a question only from retrieved evidence, cite, follow the house style. So candidates are run on the same real
tasks from the signal store and scored automatically:

- valid       structured output parsed, required fields present
- grounding   names and numbers in the output that also appear in the evidence (a hallucination proxy)
- citations   cited ids are real (precision) and enough were cited (coverage)
- style       no em/en dashes, banned words, length and sentence-count rules
- auto        0.25 valid + 0.25 grounding + 0.20 citation precision + 0.15 coverage + 0.15 style (0 if invalid)
- judges      the rules above cannot see overstatement or weak judgement, so a run can be judged (also later,
              from the saved run) by: a catalog model (blind 1-5 grades), Jev (sentence-level verdicts against the
              evidence, judges.py), or a blind grading sheet filled in by a person or by Claude in a session
              (`models blind` / `models grades`). Agreement between judges is reported.
- quality     0.5 auto + 0.5 primary judge (blind grades if present, else the first judge), or auto alone

Cost and latency come from the router's own accounting. Scores feed the `cheapest_passing` strategy through
data/model_eval.json; outputs go side by side into a Markdown report so a human can judge the writing, which
the automatic scores cannot.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import DATA_DIR, ModelsConfig, RoleSpec, load_models
from .extract import Taxonomy
from .site.prose import (ANSWER_SCHEMA, THEME_SCHEMA, answer_prompt, parse_answer, parse_theme, retrieve,
                         theme_prompt)
from .site.themes import detect_themes
from .signals import as_of_default

EVAL_DIR = DATA_DIR / "model_eval"
SCORES_FILE = DATA_DIR / "model_eval.json"
BANNED = ("delve", "leverage", "landscape", "game-changer", "game changer", "groundbreaking", "cutting-edge",
          "revolutionize", "revolutionise", "seamless", "robust")
# Questions with evidence in the corpus, plus two where the honest answer is "the sources barely cover this".
QUESTIONS = [
    "What happened around AI model risk and safety incidents at OpenAI?",
    "What is new with Claude Code and coding agents?",
    "Which decision models were released, and by whom?",
    "What are Meta's enterprise AI moves?",
    "What did Nvidia announce, and how is GPU supply changing?",
    "How are AI model prices changing between OpenAI, Anthropic and Google?",
    "What is the EU doing on AI regulation?",
    "Is anything happening with AI in banking or insurance?",
]
_CAP = re.compile(r"\b(?:[A-Z][A-Za-z0-9.+-]*[A-Za-z0-9+]|[A-Z]{2,})(?:\s+(?:[A-Z][A-Za-z0-9.+-]*|\d[\w.]*))*")
_NUM = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?(?:\s?(?:%|percent\b|x\b|billion\b|million\b|[BMK]\b))?")
_COMMON = {"The", "This", "These", "Those", "It", "Its", "In", "On", "At", "As", "A", "An", "And", "But", "Both",
           "For", "With", "While", "Meanwhile", "However", "Also", "Their", "They", "Some", "Several", "Two", "Three",
           "Most", "Many", "Recent", "New", "Why", "What", "Which", "How", "There", "No", "Not", "If", "When",
           # common sentence starters that are not names
           "Companies", "Firms", "Banks", "Insurers", "Researchers", "Users", "Developers", "Investors", "Analysts",
           "Experts", "Teams", "Models", "Agents", "Startups", "Regulators", "Enterprises", "Customers", "Prices",
           "Demand", "Revenue", "Overall", "Together", "Additionally", "Further", "Furthermore", "Instead", "Despite",
           "Although", "Because", "Since", "After", "Before", "During", "Following", "According", "Separately",
           "Elsewhere", "Similarly", "Notably", "Still", "Yet", "So", "Thus", "Then", "Now", "Today", "Earlier",
           "Later", "Last", "That", "Each", "Other", "Others", "Such", "Several", "Key", "Major", "Large", "Smaller",
           "Larger", "Both", "Neither", "Either", "Only", "Even", "More", "Less", "Fewer", "Higher", "Lower"}
_MONTHS = {m.lower() for m in ("January February March April May June July August September October November December "
                               "Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec").split()}


@dataclass
class Task:
    id: str
    role: str  # writing | reasoning
    system: str
    prompt: str
    schema: dict
    evidence: str  # titles + summaries + entity names the output may draw on
    item_ids: list[str]
    meta: dict = field(default_factory=dict)


def build_tasks(db: sqlite3.Connection, lens: str, n_themes: int = 8, questions: list[str] | None = None) -> list[Task]:
    tasks: list[Task] = []
    end = as_of_default(db)
    seen: set[tuple] = set()
    for back in range(0, 8):
        if sum(t.role == "writing" for t in tasks) >= n_themes:
            break
        for t in detect_themes(db, 14, n=3, as_of=end - timedelta(days=14 * back)):
            key = tuple(sorted(t.item_ids))
            if key in seen or len([x for x in tasks if x.role == "writing"]) >= n_themes:
                continue
            seen.add(key)
            system, prompt = theme_prompt(t, lens)
            ev = " ".join(f"{i['title']} {i.get('summary') or ''}" for i in t.items[:10]) + " " + " ".join(t.entities)
            tasks.append(Task(f"theme-{len(seen)}", "writing", system, prompt, THEME_SCHEMA, ev,
                              [i["id"] for i in t.items[:10]],
                              {"theme": t, "seed": t.seed, "kind": t.kind, "items": t.items[:10],
                               "head": f"theme seeded by {t.seed} ({t.kind})"}))
    for k, q in enumerate(questions or QUESTIONS, 1):
        ev = retrieve(db, q)
        if not ev["items"]:
            continue
        system, prompt = answer_prompt(q, lens, ev)
        text = " ".join(f"{i['title']} {i.get('summary') or ''}" for i in ev["items"]) + " " + " ".join(ev["entities"])
        tasks.append(Task(f"ask-{k}", "reasoning", system, prompt, ANSWER_SCHEMA, text,
                          [i["id"] for i in ev["items"]], {"question": q, "ev": ev, "items": ev["items"], "head": q}))
    return tasks


# ------------------------------------------------------------------------------------------- scoring


def _claims(text: str) -> list[str]:
    """Names and numbers a reader would take as facts. Dates and common sentence starters are not checked;
    a capitalised word whose lower-case form is in the evidence ("Banks" / "banks") counts as supported."""
    claims = []
    for m in _CAP.finditer(text):
        words = [w for w in m.group(0).strip(" .").split() if w.lower().strip(",.") not in _MONTHS
                 and not re.fullmatch(r"(19|20)\d\d[,.]?", w)]
        if words and words[0] in _COMMON and len(words) > 1:
            words = words[1:]  # "The Nvidia GB300" -> "Nvidia GB300"
        if not words or (len(words) == 1 and words[0] in _COMMON) or re.fullmatch(r"[0-9a-f]{16}", words[0]):
            continue
        claims.append(" ".join(words))
    for n in _NUM.finditer(text):
        tok = n.group(0).strip()
        if re.fullmatch(r"\d{1,2}|(19|20)\d\d", tok):
            continue
        claims.append(tok)
    return claims


def _supported(claim: str, evidence_lc: str) -> bool:
    c = claim.lower().strip()
    if c in evidence_lc:
        return True
    words = [w for w in re.findall(r"[a-z0-9.$%]+", c) if len(w) > 2 or any(ch.isdigit() for ch in w)]
    return bool(words) and sum(w in evidence_lc for w in words) / len(words) >= 0.67


def score(task: Task, data: dict | None, raw_text: str) -> dict:
    if not isinstance(data, dict):
        return {"valid": 0, "quality": 0.0, "auto": 0.0, "grounding": 0, "cite_precision": 0, "cite_coverage": 0, "style": 0,
                "violations": ["invalid or missing JSON"], "unsupported": []}
    violations, unsupported = [], []
    if task.role == "writing":
        out = parse_theme(data, task.meta["theme"])
        valid = int(bool(out["title"] and out["narrative"] and out["so_what"]))
        body = f"{out['title']}. {out['narrative']} {out['so_what']}"
        claim_text = f"{out['title']}. {out['narrative']}"  # so_what is about the reader, not a factual claim
        if len(out["title"].split()) > 8:
            violations.append("title longer than 8 words")
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", out["narrative"].strip()) if s]
        if len(sentences) != 2:
            violations.append(f"narrative has {len(sentences)} sentences, not 2")
        need = 2
        cited = data.get("cited_ids", [])
    else:
        out = parse_answer(data, task.meta["ev"])
        valid = int(bool(out["answer"]))
        body = claim_text = re.sub(r"\[[0-9a-f]{16}\]", "", out["answer"])
        if len(body.split()) > 170:
            violations.append(f"answer is {len(body.split())} words (limit 150)")
        need = 2
        cited = data.get("cited_ids", [])
    raw = json.dumps(data, ensure_ascii=False)
    if re.search("[—–]", raw):
        violations.append("em/en dash")
    for b in BANNED:
        if re.search(rf"\b{re.escape(b)}\b", body, re.IGNORECASE):
            violations.append(f"banned word: {b}")
    ev_lc = task.evidence.lower()
    claims = _claims(claim_text)
    unsupported = [c for c in claims if not _supported(c, ev_lc)]
    grounding = 1.0 if not claims else 1 - len(unsupported) / len(claims)
    real = [c for c in cited if c in set(task.item_ids)]
    precision = 1.0 if not cited else len(real) / len(cited)
    coverage = min(1.0, len(set(real)) / need)
    style = max(0.0, 1 - 0.25 * len(violations))
    auto = 0.0 if not valid else round(0.25 + 0.25 * grounding + 0.2 * precision + 0.15 * coverage + 0.15 * style, 3)
    return {"valid": valid, "quality": auto, "auto": auto, "grounding": round(grounding, 3), "cite_precision": round(precision, 3),
            "cite_coverage": round(coverage, 3), "style": round(style, 3), "violations": violations,
            "unsupported": unsupported[:8], "output": out}


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "faithfulness": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "usefulness": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "writing": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "problems": {"type": "string"},
    },
    "required": ["faithfulness", "usefulness", "writing", "problems"],
    "additionalProperties": False,
}
RUBRIC = """Score 1-5 (5 best):
- faithfulness: every statement is supported by the items; no overstatement (e.g. "breached" when an item says
  "hit"), no merging of unrelated stories into one claim, no facts from outside the items.
- usefulness: specific and informative for this reader; says plainly when something barely matters to them;
  does not force a connection to their field.
- writing: clear, concise, plain; follows the length and format asked for.
problems: one or two sentences naming the main problems, or "none"."""


def judge_prompt(task_prompt: str, output_text: str, lens: str) -> tuple[str, str]:
    system = ("You grade one output of a market-intelligence tool against the evidence it was given. Be strict and "
              "concrete. You do not know which model wrote it.")
    prompt = f"""Task given to the writer:
{task_prompt[:6000]}

Output to grade:
{output_text}

Reader: {lens}

{RUBRIC}"""
    return system, prompt


def output_text(role: str, out: dict | None) -> str:
    out = out or {}
    if role == "writing":
        return f"Title: {out.get('title', '')}\nNarrative: {out.get('narrative', '')}\nWhy it matters: {out.get('so_what', '')}"
    return out.get("answer", "")


def factual_text(role: str, out: dict | None) -> str:
    """The part whose facts must match the evidence (the 'why it matters' line is a judgement)."""
    out = out or {}
    return f"{out.get('title', '')}. {out.get('narrative', '')}" if role == "writing" else out.get("answer", "")


# --------------------------------------------------------------------------------------------- running


def estimate_usd(cfg: ModelsConfig, cid: str, tasks: list[Task], judging: bool = False) -> float:
    """Generous estimate: ~3.6 chars per token in, 600 tokens out per task (some models reason before answering).
    For a judge, input is the task plus an output and output is a short grade."""
    if cid in cfg.judges:
        tin = sum((len(t.prompt) / 3.6) + 900 for t in tasks)
        return tin * cfg.judges[cid].price_input_per_million / 1e6
    price = cfg.catalog[cid].price
    if price is None:
        return 0.0
    tin = sum((len(t.system) + len(t.prompt)) / 3.6 + (300 if judging else 0) for t in tasks)
    tout = (250 if judging else 600) * len(tasks)
    return (tin * price.input + tout * price.output) / 1e6


def _forced_router(cfg: ModelsConfig, role: str, cid: str, router_factory: Callable | None, job: str):
    from .calllog import CallLog
    from .llm import LLMRouter

    forced = cfg.with_overrides({role: RoleSpec(candidates=[cid])}).model_copy(
        update={"budget": cfg.budget.model_copy(update={"monthly_usd": None})})  # the eval has its own cap
    return router_factory(forced) if router_factory else LLMRouter(
        cfg=forced, log=CallLog(DATA_DIR / "llm_calls.sqlite"), job=job)


def run_candidate(cfg: ModelsConfig, cid: str, tasks: list[Task], router_factory: Callable | None = None) -> list[dict]:
    routers = {role: _forced_router(cfg, role, cid, router_factory, f"eval:{cid}") for role in {t.role for t in tasks}}
    out = []
    for t in tasks:
        try:
            res = routers[t.role].complete(t.role, t.prompt, system=t.system, json_schema=t.schema)
            sc = score(t, res.data, res.text)
            out.append({"candidate": cid, "task": t.id, "role": t.role, "ok": True, "model": f"{res.provider}/{res.model}",
                        "cost_usd": res.cost_usd, "latency_ms": res.latency_ms, "error": None,
                        "output": sc.pop("output", None), "scores": sc, "judges": {}})
        except Exception as e:  # noqa: BLE001 - a failing candidate scores 0 on that task
            sc = score(t, None, "")
            out.append({"candidate": cid, "task": t.id, "role": t.role, "ok": False, "model": None, "cost_usd": None,
                        "latency_ms": None, "error": str(e)[:300], "output": None, "scores": sc, "judges": {}})
    return out


def _task_record(t: Task) -> dict:
    items = [{"id": i["id"], "title": i["title"], "summary": i.get("summary") or ""} for i in t.meta.get("items", [])]
    return {"id": t.id, "role": t.role, "head": t.meta.get("head", t.id), "prompt": t.prompt, "items": items}


def run_eval(candidates: list[str], roles: tuple[str, ...] = ("writing", "reasoning"), n_themes: int = 6,
             max_usd: float = 2.0, confirm: Callable[[dict], bool] = lambda est: True, db=None, judge: str | None = None,
             cfg: ModelsConfig | None = None, router_factory: Callable | None = None, jev_client=None,
             workers: int = 4, log: Callable[[str], None] = lambda m: None) -> dict:
    from .extract import connect

    cfg = cfg or load_models()
    unknown = [c for c in candidates if c not in cfg.catalog]
    if unknown:
        raise ValueError(f"not in the catalog: {unknown}")
    if judge and judge not in cfg.catalog and judge not in cfg.judges:
        raise ValueError(f"judge {judge!r} is neither in the catalog nor under judges:")
    db = db if db is not None else connect()
    lens = Taxonomy.load().lens
    tasks = [t for t in build_tasks(db, lens, n_themes=n_themes) if t.role in roles]
    estimate = {c: round(estimate_usd(cfg, c, tasks), 4) for c in candidates}
    if judge:
        estimate[f"judge:{judge}"] = round(estimate_usd(cfg, judge, tasks * len(candidates), judging=True), 4)
    total = round(sum(estimate.values()), 4)
    log(f"{len(tasks)} tasks x {len(candidates)} candidates, estimated ${total:.2f}: {estimate}")
    if total > max_usd:
        raise RuntimeError(f"estimated ${total:.2f} exceeds the cap of ${max_usd:.2f}; raise --max-usd to proceed")
    if not confirm({"tasks": len(tasks), "estimate": estimate, "total": total}):
        return {"cancelled": True, "estimate": estimate}

    run = {"run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S"), "as_of": as_of_default(db).isoformat(),
           "candidates": candidates, "lens": lens, "estimate_usd": total, "tasks": [_task_record(t) for t in tasks],
           "results": [], "judge_cost": {}}
    local = [c for c in candidates if cfg.kind_of(cfg.catalog[c].provider) == "ollama"]
    remote = [c for c in candidates if c not in local]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:  # remote candidates in parallel, local after
        for rs in pool.map(lambda c: run_candidate(cfg, c, tasks, router_factory), remote):
            run["results"] += rs
            log(f"{rs[0]['candidate']}: done")
    for c in local:
        run["results"] += run_candidate(cfg, c, tasks, router_factory)
        log(f"{c}: done")
    if judge:
        apply_judge(run, judge, cfg, router_factory=router_factory, jev_client=jev_client, log=log)
    return finalize(run)


# ---------------------------------------------------------------------------------------------- judging


def apply_judge(run: dict, judge: str, cfg: ModelsConfig | None = None, router_factory: Callable | None = None,
                jev_client=None, log: Callable[[str], None] = lambda m: None) -> float:
    """Judge every successful output of a run (in place) with a catalog model or a configured judge (Jev)."""
    from .judges import JevJudge, grade_to_judge

    cfg = cfg or load_models()
    tasks = {t["id"]: t for t in run["tasks"]}
    lens = run.get("lens") or Taxonomy.load().lens
    cost = 0.0
    jev = JevJudge(cfg.judges[judge], client=jev_client) if judge in cfg.judges else None
    router = None if jev else _forced_router(cfg, "reasoning", judge, router_factory, f"eval-judge:{judge}")
    todo = [r for r in run["results"] if r["ok"] and r["output"]]
    log(f"judging {len(todo)} outputs with {judge}")
    for r in todo:
        t = tasks[r["task"]]
        try:
            if jev:
                v = jev.grade(output_text=output_text(r["role"], r["output"]), factual_text=factual_text(r["role"], r["output"]),
                              evidence=t["items"], lens=lens, kind="theme card" if r["role"] == "writing" else "answer")
                cost += v.cost_usd
                r["judges"][judge] = {"judge": v.judge, "detail": {
                    "faithfulness": v.faithfulness, "usefulness": v.usefulness, "forced_angle": v.forced_angle,
                    "flagged": [s for s in v.sentences if s["verdict"] not in ("supported", "interpretation")]}}
            else:
                system, prompt = judge_prompt(t["prompt"], output_text(r["role"], r["output"]), lens)
                res = router.complete("reasoning", prompt, system=system, json_schema=JUDGE_SCHEMA)
                cost += res.cost_usd or 0
                g = res.data or {}
                r["judges"][judge] = {"judge": grade_to_judge(g), "detail": g}
        except Exception as e:  # noqa: BLE001 - this output stays unjudged by this judge
            r["judges"][judge] = {"judge": None, "error": str(e)[:200]}
    run["judge_cost"][judge] = round(run["judge_cost"].get(judge, 0) + cost, 6)
    return cost


def primary_judge(run: dict) -> str | None:
    names = [n for r in run["results"] for n in r["judges"]]
    blind = [n for n in names if n.startswith("blind:")]
    return (blind or names or [None])[0]


def finalize(run: dict) -> dict:
    """Quality per output from the primary judge, summary, scores file, saved run and report."""
    pj = primary_judge(run)
    for r in run["results"]:
        j = (r["judges"].get(pj) or {}).get("judge") if pj else None
        auto = r["scores"]["auto"]
        r["scores"]["quality"] = 0.0 if not r["scores"]["valid"] else (round(0.5 * auto + 0.5 * j, 3) if j is not None else auto)
    run["primary_judge"] = pj
    run["summary"] = summarise(run)
    run["agreement"] = agreement(run)
    run["actual_usd"] = round(sum(r["cost_usd"] or 0 for r in run["results"]) + sum(run["judge_cost"].values()), 4)
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / f"{run['run_id']}.json").write_text(json.dumps(run, indent=1, default=str))
    (EVAL_DIR / f"{run['run_id']}.md").write_text(report(run))
    _merge_scores(run["summary"], run["run_id"])
    return {k: run[k] for k in ("run_id", "as_of", "candidates", "estimate_usd", "actual_usd", "primary_judge",
                                "summary", "agreement")}


def load_run(run_id: str | None = None) -> dict:
    runs = sorted(EVAL_DIR.glob("*.json"))
    path = EVAL_DIR / f"{run_id}.json" if run_id else (runs[-1] if runs else None)
    if not path or not path.exists():
        raise FileNotFoundError(f"no evaluation run {run_id or ''} in {EVAL_DIR}")
    return json.loads(path.read_text())


def judge_run(run_id: str | None, judge: str, **kw) -> dict:
    run = load_run(run_id)
    apply_judge(run, judge, **kw)
    return finalize(run)


# ------------------------------------------------------------------------------ blind grading by a person


def blind_sheet(run_id: str | None = None, seed: int = 7) -> dict:
    """Shuffled outputs under anonymous labels, the key in a separate file, and a grades template.

    The grader reads only blind.md (evidence + outputs) and fills blind_grades.json; no model names appear in it.
    """
    import random

    run = load_run(run_id)
    folder = EVAL_DIR / run["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    tasks = {t["id"]: t for t in run["tasks"]}
    todo = [r for r in run["results"] if r["ok"] and r["output"]]
    random.Random(seed).shuffle(todo)
    key, template = {}, {}
    lines = [f"# Blind grading sheet, run {run['run_id']}", "", RUBRIC, "",
             "Grade each output against its own evidence. Model names are hidden.", ""]
    for task_id in sorted({r["task"] for r in todo}):
        t = tasks[task_id]
        lines += [f"## Task {task_id}: {t['head']}", "", "Evidence:"]
        lines += [f"- [{i['id']}] {i['title']}: {i['summary'][:400]}" for i in t["items"]]
        lines.append("")
        for r in [r for r in todo if r["task"] == task_id]:
            label = f"X{len(key) + 1:03d}"
            key[label] = {"candidate": r["candidate"], "task": task_id}
            template[label] = {"faithfulness": None, "usefulness": None, "writing": None, "problems": ""}
            lines += [f"### {label}", "", output_text(r["role"], r["output"]), ""]
    (folder / "blind.md").write_text("\n".join(lines))
    (folder / "blind_key.json").write_text(json.dumps(key, indent=1))
    grades = folder / "blind_grades.json"
    if not grades.exists():
        grades.write_text(json.dumps(template, indent=1))
    return {"sheet": str(folder / "blind.md"), "grades": str(grades), "outputs": len(key)}


def import_grades(run_id: str | None = None, grader: str = "human") -> dict:
    from .judges import grade_to_judge

    run = load_run(run_id)
    folder = EVAL_DIR / run["run_id"]
    key = json.loads((folder / "blind_key.json").read_text())
    grades = json.loads((folder / "blind_grades.json").read_text())
    name = f"blind:{grader}"
    by = {(r["candidate"], r["task"]): r for r in run["results"]}
    done = 0
    for label, g in grades.items():
        if not all(isinstance(g.get(k), int) and 1 <= g[k] <= 5 for k in ("faithfulness", "usefulness", "writing")):
            continue
        r = by[(key[label]["candidate"], key[label]["task"])]
        r["judges"][name] = {"judge": grade_to_judge(g), "detail": g}
        done += 1
    if not done:
        raise ValueError("no complete grades in blind_grades.json (each needs faithfulness, usefulness, writing 1-5)")
    run["judge_cost"].setdefault(name, 0.0)
    return {**finalize(run), "graded": done}


# ------------------------------------------------------------------------------------------- summaries


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return round(cov / (va * vb) ** 0.5, 3) if va and vb else None


def agreement(run: dict) -> list[dict]:
    """For each pair of judges: rank correlation over the outputs both judged, and mean absolute difference."""
    names = sorted({n for r in run["results"] for n in r["judges"]})
    out = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pairs = [(r["judges"][a]["judge"], r["judges"][b]["judge"]) for r in run["results"]
                     if (r["judges"].get(a) or {}).get("judge") is not None and (r["judges"].get(b) or {}).get("judge") is not None]
            if pairs:
                xs, ys = zip(*pairs, strict=True)
                out.append({"judges": [a, b], "n": len(pairs), "spearman": spearman(list(xs), list(ys)),
                            "mean_abs_diff": round(statistics.mean(abs(x - y) for x, y in pairs), 3)})
    return out


def summarise(run: dict) -> dict:
    by: dict[tuple[str, str], list[dict]] = {}
    for r in run["results"]:
        by.setdefault((r["role"], r["candidate"]), []).append(r)
    judges = sorted({n for r in run["results"] for n in r["judges"]})
    summary: dict[str, dict] = {}
    for (role, cid), rs in by.items():
        costs = [r["cost_usd"] for r in rs if r["cost_usd"] is not None]
        lat = [r["latency_ms"] for r in rs if r["latency_ms"]]
        entry = {
            "quality": round(statistics.mean(r["scores"]["quality"] for r in rs), 3),
            "auto": round(statistics.mean(r["scores"]["auto"] for r in rs), 3),
            "grounding": round(statistics.mean(r["scores"]["grounding"] for r in rs), 3),
            "valid_rate": round(statistics.mean(r["scores"]["valid"] for r in rs), 3),
            "cost_per_task": round(statistics.mean(costs), 6) if costs else None,
            "latency_p50_ms": int(statistics.median(lat)) if lat else None,
            "failures": sum(not r["ok"] for r in rs), "tasks": len(rs),
        }
        for j in judges:
            vals = [(r["judges"].get(j) or {}).get("judge") for r in rs]
            vals = [v for v in vals if v is not None]
            entry[f"judge:{j}"] = round(statistics.mean(vals), 3) if vals else None
        summary.setdefault(role, {})[cid] = entry
    return summary


def _merge_scores(summary: dict, run_id: str) -> None:
    """Latest score per role and candidate, kept across runs (a run may test only some candidates)."""
    current = json.loads(SCORES_FILE.read_text()) if SCORES_FILE.exists() else {"scores": {}}
    for role, cands in summary.items():
        for cid, s in cands.items():
            current["scores"].setdefault(role, {})[cid] = {**s, "run_id": run_id}
    current["updated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    SCORES_FILE.write_text(json.dumps(current, indent=1))


def report(run: dict) -> str:
    judges = sorted({n for r in run["results"] for n in r["judges"]})
    lines = [f"# Model evaluation {run['run_id']}", "",
             f"Data to {run['as_of']}. Primary judge: {run.get('primary_judge') or 'none (automatic checks only)'}. "
             f"Estimated ${run['estimate_usd']:.2f}, actual ${run.get('actual_usd', 0):.4f}.", ""]
    for j in judges:
        if j in run["candidates"]:
            lines += [f"Note: {j} judged its own outputs too; self-preference may inflate its score.", ""]
    for a in run.get("agreement", []):
        lines.append(f"- Agreement {a['judges'][0]} vs {a['judges'][1]}: Spearman {a['spearman']}, "
                     f"mean difference {a['mean_abs_diff']} over {a['n']} outputs")
    lines.append("")
    for role, cands in run["summary"].items():
        jcols = [f"judge:{j}" for j in judges]
        lines += [f"## {role}", "", "| candidate | quality | auto | " + " | ".join(jcols) + (" | " if jcols else "")
                  + "grounding | valid | $/task | p50 latency | failures |",
                  "|---" * (8 + len(jcols)) + "|"]
        for cid, s in sorted(cands.items(), key=lambda kv: -kv[1]["quality"]):
            cost = "n/a" if s["cost_per_task"] is None else f"{s['cost_per_task']:.5f}"
            lat = "n/a" if s["latency_p50_ms"] is None else f"{s['latency_p50_ms'] / 1000:.1f}s"
            jv = " | ".join("n/a" if s.get(c) is None else f"{s[c]:.2f}" for c in jcols)
            lines.append(f"| {cid} | {s['quality']:.2f} | {s['auto']:.2f} | " + (jv + " | " if jcols else "")
                         + f"{s['grounding']:.2f} | {s['valid_rate']:.0%} | {cost} | {lat} | {s['failures']}/{s['tasks']} |")
        lines.append("")
    lines += ["## Outputs side by side", ""]
    for t in run["tasks"]:
        lines += [f"### {t['id']}: {t['head']}", ""]
        for r in sorted([r for r in run["results"] if r["task"] == t["id"]], key=lambda r: -r["scores"]["quality"]):
            o = r["output"] or {}
            text = (f"**{o.get('title', '')}** {o.get('narrative', '')} *{o.get('so_what', '')}*" if r["role"] == "writing"
                    else o.get("answer", ""))
            flags = list(r["scores"]["violations"])
            if r["scores"].get("unsupported"):
                flags.append(f"unsupported: {', '.join(r['scores']['unsupported'])}")
            for j, v in r["judges"].items():
                d = v.get("detail") or {}
                if "flagged" in d:
                    bad = "; ".join(f"{s['verdict']}: {s['sentence'][:80]}" for s in d["flagged"][:3])
                    flags.append(f"{j} {v['judge']}: {bad or 'all supported'}")
                elif d:
                    flags.append(f"{j} F{d.get('faithfulness')} U{d.get('usefulness')} W{d.get('writing')}: {d.get('problems', '')}")
                elif v.get("error"):
                    flags.append(f"{j}: error {v['error'][:80]}")
            lines.append(f"- **{r['candidate']}** (q {r['scores']['quality']:.2f})"
                         f"{' ERROR ' + r['error'] if r['error'] else ''}: {text}" + (f"  \n  _{' | '.join(flags)}_" if flags else ""))
        lines.append("")
    return "\n".join(lines)


def scores_table(cfg: ModelsConfig | None = None) -> list[dict]:
    cfg = cfg or load_models()
    scores = json.loads(SCORES_FILE.read_text())["scores"] if SCORES_FILE.exists() else {}
    rows = []
    for cid, e in cfg.catalog.items():
        rows.append({"id": cid, "model": f"{e.provider}/{e.model}",
                     "price": None if e.price is None else f"{e.price.input:g}/{e.price.output:g}",
                     "data_policy": e.data_policy,
                     **{f"{role}_quality": scores.get(role, {}).get(cid, {}).get("quality") for role in ("writing", "reasoning")},
                     **{f"{role}_cost": scores.get(role, {}).get(cid, {}).get("cost_per_task") for role in ("writing", "reasoning")}})
    return rows
