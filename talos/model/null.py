# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""NullJudge: no model. Returns an 'allow' distribution with no evidence. Used for shadow mode / fastpath-only runs."""
from __future__ import annotations

from .base import Answers


class NullJudge:
    name = "null"

    def judge(self, body: dict, deadline_s: float | None = None) -> Answers:
        q = body.get("questions", {})
        out: Answers = {}
        for qid, spec in q.items():
            if spec["type"] == "noul":
                out[qid] = 0.0
            elif spec["type"] == "choice":
                keys = list(spec["criteria"])
                out[qid] = {k: (1.0 if k == keys[0] else 0.0) for k in keys}
            else:
                out[qid] = {"score": 0.0, "probabilities": {"0": 1.0}}
        return out
