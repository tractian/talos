# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""OnnxJudge: the Talos student (a small encoder over the talos/render_v01 symbol tokens) exported to ONNX.

Artifacts next to the model (shipped together as the weights; `talos-pull` fetches them):
  student.int8.onnx / student.onnx   input  ids [B, L] int64 (= render_v01.pack(tokens): [CLS], head markers, tokens)
                                     outputs one per head, named by head: noul [B,1] logit, choice/score [B,K] logits
  vocab.json    {"tokens": {token: id}, "pad", "unk", "cls", "max_len"}
  heads.json    {"heads": {head: {"kind": noul|choice|score, "classes": [...], "T": temperature}}, "qhat", "order"}

The judge renders with talos.render_v01 (the training renderer), so the gate must hand it tokens from that renderer:
`wants == "render_v01"` tells talos/gate.py which one to use. Answers come back in the talos.model.base shape.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .base import Answers, JudgeError

MODEL_NAMES = ("student.int8.onnx", "student.onnx", "model.onnx")


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - x.max()
    e = np.exp(x)
    return e / e.sum()


class OnnxJudge:
    name = "onnx"
    wants = "render_v01"

    def __init__(self, path: str, threads: int = 1):
        p = Path(path)
        if p.is_dir():
            model_file = next((p / n for n in MODEL_NAMES if (p / n).exists()), p / MODEL_NAMES[0])
            d = p
        else:
            model_file, d = p, p.parent
        vocab_file, heads_file = d / "vocab.json", d / "heads.json"
        missing = [str(f) for f in (model_file, vocab_file, heads_file) if not f.exists()]
        if missing:
            raise JudgeError(f"ONNX judge artifacts missing: {missing}")
        try:
            import onnxruntime as ort
        except ImportError as e:
            raise JudgeError("onnxruntime not installed") from e
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(model_file), so, providers=["CPUExecutionProvider"])
        v = json.loads(vocab_file.read_text())
        self.tokens: dict[str, int] = v["tokens"]
        self.supports_fp = "FP_SEEN_ANY=yes" in self.tokens   # trained with register fingerprints (map-free mode)?
        self.pad, self.unk, self.cls = int(v.get("pad", 0)), int(v.get("unk", 1)), int(v.get("cls", 2))
        self.max_len = int(v.get("max_len", 160))
        h = json.loads(heads_file.read_text())
        self.heads: dict[str, dict] = h["heads"]
        self.order: list[str] = h.get("order") or list(self.heads)
        self.qhat = h.get("qhat")
        self.inputs = [i.name for i in self.sess.get_inputs()]
        self.outputs = [o.name for o in self.sess.get_outputs()]
        self.path = str(model_file)

    # --- encoding: exactly the trainer's pack(): [CLS] + one marker per head (training order) + tokens, cut at max_len
    def encode(self, tokens: list[str]) -> np.ndarray:
        ids = [self.cls] + [self.tokens.get(f"[Q_{q}]", self.unk) for q in self.order] + [self.tokens.get(t, self.unk) for t in tokens]
        return np.asarray([ids[: self.max_len]], dtype=np.int64)

    def raw(self, tokens: list[str]) -> dict[str, np.ndarray]:
        ids = self.encode(tokens)
        feeds = {self.inputs[0]: ids}
        if len(self.inputs) > 1:      # a graph exported with a mask input
            feeds[self.inputs[1]] = np.ones_like(ids)
        outs = self.sess.run(None, feeds)
        return {n: np.asarray(o)[0].astype(np.float64) for n, o in zip(self.outputs, outs)}

    def judge_tokens(self, tokens: list[str]) -> Answers:
        by_name = self.raw(tokens)
        ans: Answers = {}
        for q, spec in self.heads.items():
            if q not in by_name:
                continue
            z = by_name[q].reshape(-1) / float(spec.get("T", 1.0) or 1.0)
            kind, classes = spec["kind"], spec.get("classes")
            if kind == "noul":
                ans[q] = float(1.0 / (1.0 + math.exp(-float(z[0]))))
            elif kind == "choice":
                p = _softmax(z[: len(classes)])
                ans[q] = {str(k): float(x) for k, x in zip(classes, p)}
            else:
                p = _softmax(z[: len(classes)])
                ans[q] = {"score": float(sum(float(k) * x for k, x in zip(classes, p))),
                          "probabilities": {str(k): float(x) for k, x in zip(classes, p)}}
        return ans

    def judge(self, body: dict, deadline_s: float | None = None) -> Answers:
        toks = body.get("tokens")
        if toks is None:
            raise JudgeError("OnnxJudge needs body['tokens'] (render_v01 symbol tokens); the gate supplies them")
        return self.judge_tokens(toks)
