"""Judges for model evaluation (ADR-0013): Jev (TypeSafe System One) and human/in-session blind grading.

Jev does not write critiques; it answers narrow typed questions with probabilities. So the judge follows
TypeSafe's citation-check pattern instead of asking for an overall grade:
- faithfulness: code splits the output into sentences; one Choice per sentence compares it with the evidence
  items (supported / overstated / contradicted / unsupported / interpretation), all in one request
- usefulness: one Score with concrete situations as levels
- forced angle: one Noul, does the output claim a link to the reader's field that the items do not make

Each level is written to stand alone (Jev scores levels separately and never sees their numbers).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .config import JudgeSpec
from .llm import _keychain

FACT_VALUE = {"supported": 1.0, "interpretation": 0.8, "overstated": 0.4, "unsupported": 0.1, "contradicted": 0.0}

SENTENCE_CRITERIA = {
    "supported": "The evidence items state this or directly imply it, with the same strength and scope.",
    "overstated": ("The evidence items support only a weaker, narrower or less certain version: the statement "
                   "exaggerates scale, severity, certainty or wording, or merges separate stories into one claim."),
    "contradicted": "The evidence items say the opposite.",
    "unsupported": "The evidence items do not address this fact at all: it comes from outside knowledge or invention.",
    "interpretation": ("The statement is a judgement about relevance or implications for the reader, not a "
                       "factual claim about events."),
}

USEFULNESS_LEVELS = [
    "The text restates headlines in general terms with almost no specific names, numbers or facts.",
    "The text gives specific facts but no sense of what they mean or for whom.",
    "The text gives specific facts and a sound, modest statement of relevance, including saying plainly when "
    "relevance to the reader is low.",
    "The text gives specific, accurate facts and names a concrete implication for financial-services risk, "
    "compliance or architecture work that the evidence items support.",
]


def split_sentences(text: str, limit: int = 12) -> list[str]:
    text = re.sub(r"\s*\[[0-9a-f]{16}\]", "", text)
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text) if len(p.strip()) > 3]
    return parts[:limit]


@dataclass
class JevVerdict:
    judge: float          # 0..1, comparable with the LLM judge's normalised score
    faithfulness: float   # 0..1, mean value of sentence verdicts
    usefulness: float     # 0..1
    forced_angle: float   # probability 0..1
    sentences: list[dict]
    input_tokens: int
    cost_usd: float


class JevJudge:
    def __init__(self, spec: JudgeSpec, client=None):
        self.spec = spec
        if client is None:
            from typesafe_sdk import TypeSafeClient

            key = os.environ.get(spec.api_key_env) or _keychain(spec.api_key_env)
            if not key:
                raise RuntimeError(f"no TypeSafe key: set {spec.api_key_env} in .env or the keychain "
                                   f"(security add-generic-password -s signal-radar -a {spec.api_key_env} -w)")
            client = TypeSafeClient(api_key=key, model=spec.model)
        self.client = client

    def grade(self, *, output_text: str, factual_text: str, evidence: list[dict], lens: str,
              kind: str) -> JevVerdict:
        from typesafe_sdk import Choice, Noul, Score

        sentences = split_sentences(factual_text)
        state = {
            "reader": lens,
            "output_kind": kind,
            "output": output_text,
            "evidence": [{"id": e["id"], "title": e["title"], "summary": (e.get("summary") or "")[:600]}
                         for e in evidence[:12]],
        }
        questions = {
            f"s{i}": Choice(
                instructions={"task": "Compare this statement from `output` with the items in `evidence`.",
                              "statement": s},
                criteria=SENTENCE_CRITERIA)
            for i, s in enumerate(sentences)
        }
        questions["usefulness"] = Score(
            instructions="How useful is `output` for the person described in `reader`, given `evidence`?",
            criteria=USEFULNESS_LEVELS)
        questions["forced_angle"] = Noul(
            instructions=("Does `output` claim a connection to banking, insurance, payments, regulation or compliance "
                          "that none of the items in `evidence` mention?"))
        resp = self.client.system_one(state=state, questions=questions)
        verdicts = []
        for i, s in enumerate(sentences):
            a = resp.choices[f"s{i}"]
            probs = dict(a.probabilities)
            value = sum(FACT_VALUE[k] * p for k, p in probs.items() if k in FACT_VALUE)
            verdicts.append({"sentence": s, "verdict": a.choice, "confidence": round(a.confidence, 3),
                             "value": round(value, 3)})
        faith = sum(v["value"] for v in verdicts) / len(verdicts) if verdicts else 1.0
        useful = resp.scores["usefulness"].score / (len(USEFULNESS_LEVELS) - 1)
        forced = resp.nouls["forced_angle"].noul
        judge = 0.6 * faith + 0.3 * useful + 0.1 * (1 - forced)
        tokens = (resp.usage.input_tokens or 0) if resp.usage else 0
        return JevVerdict(round(judge, 3), round(faith, 3), round(useful, 3), round(forced, 3), verdicts, tokens,
                          round(tokens * self.spec.price_input_per_million / 1e6, 6))


def grade_to_judge(g: dict) -> float:
    """1-5 grades (LLM judge or blind human/in-session grading) on the same 0..1 scale as Jev's judge."""
    return round((g["faithfulness"] * 0.5 + g["usefulness"] * 0.3 + g["writing"] * 0.2 - 1) / 4, 3)
