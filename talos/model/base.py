# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Judge protocol. A judge takes a System One body ({state, questions}) and returns normalized answers:

    {"action": {"allow": p, "cache": p, "defer": p, "deny": p},     # choice -> distribution over criteria keys
     "needed_now": p_true, "duplicate": p_true, ...,                 # noul -> P(true)
     "envelope": {"inside_typical": p, ...},
     "severity": {"score": 1.3, "probabilities": {"0": p, ...}}}    # score -> expectation + distribution
"""
from __future__ import annotations

from typing import Any, Protocol

Answers = dict[str, Any]


class JudgeError(RuntimeError):
    pass


class JudgeTimeout(JudgeError):
    pass


class Judge(Protocol):
    name: str

    def judge(self, body: dict, deadline_s: float | None = None) -> Answers: ...


def normalize_answers(raw: dict, questions: dict) -> Answers:
    """Accept the Jev / Kev response shape ({"answers": {id: {...}}} or {id: {...}}) and normalize."""
    ans = raw.get("answers", raw)
    out: Answers = {}
    for qid, q in questions.items():
        a = ans.get(qid)
        if a is None:
            continue
        t = q["type"]
        if t == "noul":
            if isinstance(a, dict):
                p = a.get("noul", a.get("probabilities", {}).get("true"))
            else:
                p = a
            out[qid] = float(p) if p is not None else None
        elif t == "choice":
            probs = a.get("probabilities") if isinstance(a, dict) else None
            if probs is None and isinstance(a, dict) and "choice" in a:
                probs = {k: (1.0 if k == a["choice"] else 0.0) for k in q["criteria"]}
            elif probs is None and isinstance(a, str):
                probs = {k: (1.0 if k == a else 0.0) for k in q["criteria"]}
            out[qid] = {k: float(probs.get(k, 0.0)) for k in q["criteria"]} if probs else None
        elif t == "score":
            if isinstance(a, dict):
                probs = a.get("probabilities") or {}
                score = a.get("score")
                if score is None and probs:
                    score = sum(int(k) * float(v) for k, v in probs.items())
                out[qid] = {"score": float(score) if score is not None else None,
                            "probabilities": {str(k): float(v) for k, v in probs.items()}}
            else:
                out[qid] = {"score": float(a), "probabilities": {}}
    return out
