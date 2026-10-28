"""Model evaluation: scoring rules, cost cap, judges (catalog, Jev, blind grading) and agreement (ADR-0013)."""

import json
from types import SimpleNamespace

import pytest

from signal_radar import evals
from signal_radar.config import ModelsConfig
from signal_radar.judges import JevJudge, grade_to_judge, split_sentences
from signal_radar.site.prose import ANSWER_SCHEMA
from signal_radar.site.themes import Theme
from test_model_selection import CFG

ITEMS = [{"id": "a" * 16, "date": "2026-10-01", "source": "tldr_ai", "title": "Stripe ships MCP server for banks",
          "summary": "Stripe released an MCP server; 40% of pilots are banks.", "url": "https://x/a"},
         {"id": "b" * 16, "date": "2026-10-02", "source": "alphasignal", "title": "Anthropic expands MCP",
          "summary": "Anthropic added MCP tools for payments.", "url": "https://x/b"}]
THEME = Theme(kind="momentum", seed="MCP", entities=["MCP", "Stripe", "Anthropic"], item_ids=["a" * 16, "b" * 16],
              score=1, families=2, avg_relevance=2, confidence="medium", items=ITEMS)
JCFG = {**CFG, "judges": {"jev": {"kind": "typesafe", "model": "jev-test", "api_key_env": "TS",
                                  "price_input_per_million": 0.042}}}


def theme_task():
    ev = " ".join(f"{i['title']} {i['summary']}" for i in ITEMS) + " MCP Stripe Anthropic"
    return evals.Task("theme-1", "writing", "sys", "prompt", {}, ev, [i["id"] for i in ITEMS],
                      {"theme": THEME, "items": ITEMS, "head": "theme seeded by MCP"})


def ask_task():
    ev = {"items": ITEMS, "entities": [], "trends": []}
    text = " ".join(f"{i['title']} {i['summary']}" for i in ITEMS)
    return evals.Task("ask-1", "reasoning", "sys", "prompt", ANSWER_SCHEMA, text, [i["id"] for i in ITEMS],
                      {"question": "q", "ev": ev, "items": ITEMS, "head": "q"})


GOOD = {"title": "Stripe and Anthropic push MCP into payments",
        "narrative": "Stripe released an MCP server used in bank pilots. Anthropic added MCP tools for payments.",
        "so_what": "Banks can test agents against payment rails.", "cited_ids": ["a" * 16, "b" * 16]}
BAD = dict(GOOD, narrative="Visa bought Stripe. Banks reacted.")


def test_good_output_scores_high_and_rules_catch_problems():
    good = evals.score(theme_task(), GOOD, json.dumps(GOOD))
    assert good["valid"] == 1 and good["grounding"] == 1.0 and good["auto"] >= 0.95 and not good["violations"]
    bad = dict(GOOD, narrative="Visa acquired Stripe for $90 billion — a landmark deal. Banks reacted.",
               cited_ids=["a" * 16, "f" * 16])
    s = evals.score(theme_task(), bad, json.dumps(bad))
    assert "Visa" in s["unsupported"] and "$90 billion" in s["unsupported"]
    assert "em/en dash" in s["violations"] and s["cite_precision"] == 0.5 and s["auto"] < good["auto"]
    assert evals.score(theme_task(), None, "garbage")["auto"] == 0


def test_answer_length_and_citations():
    long = {"answer": "Stripe released an MCP server [" + "a" * 16 + "]. " + "word " * 200, "cited_ids": ["a" * 16],
            "evidence_is_thin": False}
    s = evals.score(ask_task(), long, json.dumps(long))
    assert any("words" in v for v in s["violations"]) and s["cite_coverage"] == 0.5


def fake_factory(outputs_by_model, costs=None):
    def factory(cfg):
        def complete(role, prompt, system=None, json_schema=None):
            cand = cfg.roles[role].candidates[0]  # the eval forces exactly one candidate on the role it calls
            if json_schema is evals.JUDGE_SCHEMA:
                data = {"faithfulness": 4, "usefulness": 3, "writing": 5, "problems": "none"}
            else:
                data = outputs_by_model[cand]
                if isinstance(data, Exception):
                    raise data
            return SimpleNamespace(data=data, text=json.dumps(data), provider="hub", model=cand,
                                   cost_usd=(costs or {}).get(cand, 0.001), latency_ms=10)
        return SimpleNamespace(complete=complete)
    return factory


class FakeJev:
    """Stands in for TypeSafeClient: marks sentences mentioning Visa as unsupported."""

    def system_one(self, state, questions):
        choices = {}
        for name, q in questions.items():
            if name.startswith("s"):
                bad = "Visa" in q.instructions["statement"]
                probs = {"supported": 0.05 if bad else 0.9, "unsupported": 0.9 if bad else 0.05, "overstated": 0.05,
                         "contradicted": 0.0, "interpretation": 0.0}
                choices[name] = SimpleNamespace(choice=max(probs, key=probs.get), confidence=0.9, probabilities=probs)
        return SimpleNamespace(choices=choices, scores={"usefulness": SimpleNamespace(score=2.0)},
                               nouls={"forced_angle": SimpleNamespace(noul=0.1)},
                               usage=SimpleNamespace(input_tokens=2000))


@pytest.fixture
def eval_env(tmp_path, monkeypatch):
    monkeypatch.setattr(evals, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(evals, "SCORES_FILE", tmp_path / "model_eval.json")
    monkeypatch.setattr(evals, "DATA_DIR", tmp_path)
    monkeypatch.setattr(evals, "build_tasks", lambda db, lens, n_themes=6, questions=None: [theme_task()])
    monkeypatch.setattr(evals, "as_of_default", lambda db: __import__("datetime").date(2026, 10, 2))
    return tmp_path


def test_run_eval_with_catalog_judge_writes_report_and_scores(eval_env):
    cfg = ModelsConfig.model_validate(JCFG)
    outputs = {"cheap": GOOD, "pricey": BAD, "local": RuntimeError("ollama down")}
    rec = evals.run_eval(["cheap", "pricey", "local"], roles=("writing",), db=object(), cfg=cfg, judge="pricey",
                         router_factory=fake_factory(outputs, costs={"cheap": 0.0002, "pricey": 0.02}), max_usd=5)
    w = rec["summary"]["writing"]
    assert w["cheap"]["quality"] > w["pricey"]["quality"] and w["local"]["failures"] == 1 and w["local"]["quality"] == 0
    assert w["cheap"]["judge:pricey"] == grade_to_judge({"faithfulness": 4, "usefulness": 3, "writing": 5})
    report = (eval_env / "eval" / f"{rec['run_id']}.md").read_text()
    assert "judged its own outputs" in report and "Visa" in report
    scores = json.loads((eval_env / "model_eval.json").read_text())["scores"]
    assert [c for c, _ in cfg.resolve("writing", scores)][0] == "cheap"  # passes the bar and is cheapest


def test_jev_judge_flags_unsupported_sentences_and_can_rejudge_a_saved_run(eval_env):
    cfg = ModelsConfig.model_validate(JCFG)
    rec = evals.run_eval(["cheap", "pricey"], roles=("writing",), db=object(), cfg=cfg,
                         router_factory=fake_factory({"cheap": GOOD, "pricey": BAD}))
    assert rec["primary_judge"] is None  # auto scores only
    out = evals.judge_run(rec["run_id"], "jev", cfg=cfg, jev_client=FakeJev())
    w = out["summary"]["writing"]
    assert w["cheap"]["judge:jev"] > w["pricey"]["judge:jev"] and out["primary_judge"] == "jev"
    run = evals.load_run(rec["run_id"])
    flagged = [r["judges"]["jev"]["detail"]["flagged"] for r in run["results"] if r["candidate"] == "pricey"][0]
    assert flagged and "Visa" in flagged[0]["sentence"]
    assert run["judge_cost"]["jev"] == pytest.approx(2 * 2000 * 0.042 / 1e6)


def test_blind_sheet_hides_names_and_grades_become_primary_judge(eval_env):
    cfg = ModelsConfig.model_validate(JCFG)
    rec = evals.run_eval(["cheap", "pricey"], roles=("writing",), db=object(), cfg=cfg,
                         router_factory=fake_factory({"cheap": GOOD, "pricey": BAD}))
    evals.judge_run(rec["run_id"], "jev", cfg=cfg, jev_client=FakeJev())
    sheet = evals.blind_sheet(rec["run_id"])
    text = open(sheet["sheet"]).read()
    assert "cheap" not in text and "pricey" not in text and "X001" in text and "Evidence:" in text
    key = json.loads(open(sheet["grades"].replace("blind_grades", "blind_key")).read())
    grades = {label: ({"faithfulness": 5, "usefulness": 4, "writing": 4, "problems": "none"} if v["candidate"] == "cheap"
                      else {"faithfulness": 1, "usefulness": 2, "writing": 3, "problems": "invents an acquisition"})
              for label, v in key.items()}
    open(sheet["grades"], "w").write(json.dumps(grades))
    out = evals.import_grades(rec["run_id"], grader="claude")
    assert out["graded"] == 2 and out["primary_judge"] == "blind:claude"
    assert out["summary"]["writing"]["cheap"]["quality"] > out["summary"]["writing"]["pricey"]["quality"]
    assert any(set(a["judges"]) == {"jev", "blind:claude"} for a in out["agreement"])


def test_run_eval_refuses_over_cap_and_respects_cancel(eval_env):
    cfg = ModelsConfig.model_validate(JCFG)
    with pytest.raises(RuntimeError, match="exceeds the cap"):
        evals.run_eval(["pricey"], roles=("writing",), db=object(), cfg=cfg, max_usd=0.0000001,
                       router_factory=fake_factory({"pricey": GOOD}))
    rec = evals.run_eval(["cheap"], roles=("writing",), db=object(), cfg=cfg, confirm=lambda est: False,
                         router_factory=fake_factory({"cheap": GOOD}))
    assert rec["cancelled"] and not (eval_env / "model_eval.json").exists()


def test_spearman_and_sentence_split():
    assert evals.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert evals.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert evals.spearman([1, 2], [1, 2]) is None
    assert split_sentences("Stripe shipped it [" + "a" * 16 + "]. Banks reacted. 40% of pilots are banks.") == [
        "Stripe shipped it.", "Banks reacted.", "40% of pilots are banks."]


def test_jev_requires_a_key(monkeypatch):
    monkeypatch.delenv("TS", raising=False)
    monkeypatch.setattr("signal_radar.judges._keychain", lambda account: None)
    with pytest.raises(RuntimeError, match="no TypeSafe key"):
        JevJudge(ModelsConfig.model_validate(JCFG).judges["jev"])
