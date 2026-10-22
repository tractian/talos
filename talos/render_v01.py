# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Closed-vocabulary rendering of (Request, Context): the rendering the v0.1 teacher and student were trained on, and the
one the live gate feeds the ONNX student (talos/model/onnx.py). Contract (docs/INJECTION.md, docs/SCHEMA.md): no free
text, no IPs, no timestamps, no strings; every number that matters is a word from VOCAB.

  to_text(req, ctx, mode)   -> compact text for the Kev-style teacher (still closed vocabulary)
  to_tokens(req, ctx, mode) -> list of symbol tokens for the student (all in VOCAB)
  mode: "declared" (register semantics from the map: cls/safe/wwr/asset/state/limitfrac/env; the default and what the
  v0.1 gate renders), "declared_fp" (declared + the register fingerprint appended), "fingerprint" (map-free: the semantic
  fields are replaced by what the gateway observed about the register; see talos.schema.Fingerprint)
  pack(tokens)        -> ids as the student sees them: [CLS] + one marker per head (HEADS order) + encoded tokens, cut at MAX_LEN
"""
from __future__ import annotations

from .schema import Context, Request

MAX_HISTORY = 8


def _bin_delta(d):
    if d is None:
        return "na"
    a = abs(d)
    s = "+" if d > 0 else "-" if d < 0 else ""
    if a < 0.5:
        return "0"
    if a < 2:
        return s + "tiny"
    if a < 5:
        return s + "small"
    if a < 15:
        return s + "mid"
    if a < 40:
        return s + "large"
    return s + "huge"


def _bin_frac(f):
    if f is None:
        return "na"
    if f < 0:
        return "below"
    if f > 1:
        return "above"
    return ["q0", "q1", "q2", "q3", "q4"][min(4, int(f * 5))]


def _bin_age(a):
    if a is None:
        return "na"
    if a < 1:
        return "fresh"
    if a < 10:
        return "recent"
    if a < 60:
        return "old"
    return "stale"


def _bin_count(n, edges=(0, 1, 3, 10, 30, 100)):
    for i, e in enumerate(edges):
        if n <= e:
            return f"c{i}"
    return f"c{len(edges)}"


def _bin_budget(f):
    if f < 0.25:
        return "low"
    if f < 0.5:
        return "half"
    if f < 1.0:
        return "high"
    if f < 2.0:
        return "over"
    return "flood"


MODES = ("declared", "declared_fp", "fingerprint")


def _fp_fields(req: Request, ctx: Context) -> list[tuple[str, str]]:
    fp = ctx.fingerprint
    if fp is None:
        return []
    f = [("fp_writers", _bin_count(fp.writers)), ("fp_hmi", "yes" if fp.hmi_writes else "no"),
         ("fp_w10m", _bin_count(fp.writes_10m)), ("fp_moves", fp.moves),
         ("fp_lastwrite", "never" if not fp.ever_written else _bin_age(fp.last_write_age_s)),
         ("fp_seen_me", "yes" if fp.seen_me else "no"), ("fp_seen_any", "yes" if fp.seen_any else "no")]
    if req.op.value == "write":
        f += [("fp_range", fp.range_pos), ("fp_delta", _bin_delta(fp.delta_obs_pct))]
    return f


def fields(req: Request, ctx: Context, mode: str = "declared") -> list[tuple[str, str]]:
    """Ordered (key, value) pairs; both renderings derive from this."""
    if mode not in MODES:
        raise ValueError(f"unknown render mode {mode!r}")
    t = req.target
    blind = mode == "fingerprint"
    f = [("op", req.op.value), ("proto", req.protocol.value)]
    if not blind:
        f += [("cls", t.cls.value), ("safe", t.safe_direction.value), ("wwr", "yes" if t.writable_while_running else "no")]
    f += [("count", _bin_count(req.count, (1, 2, 8, 32, 128)))]
    if not blind:
        f += [("asset", ctx.asset_kind), ("state", ctx.asset_state.value)]
    f += [("now", "present" if ctx.target_now.value is not None else "absent"), ("age", _bin_age(ctx.target_now.age_s))]
    w = ctx.write
    if req.op.value == "write":
        if blind:   # delta/rate/cum/drift are vs the OBSERVED range in this mode (the builder computes them so); no limits, no envelope
            f += [("delta", _bin_delta(w.delta_pct)), ("rate", w.implied_rate), ("cum", w.cum_delta_bin), ("drift", w.drift),
                  ("dup", "yes" if w.duplicate else "no")]
        else:
            f += [("delta", _bin_delta(w.delta_pct)), ("limitfrac", _bin_frac(w.limit_frac)), ("env", w.envelope_pos),
                  ("rate", w.implied_rate), ("cum", w.cum_delta_bin), ("drift", w.drift), ("dup", "yes" if w.duplicate else "no")]
    if mode in ("declared_fp", "fingerprint"):
        f += _fp_fields(req, ctx)
    a = ctx.agent
    f += [("tier", a.tier.value), ("role", a.role), ("rep", a.rep_bin), ("reqs1m", _bin_count(a.reqs_1m)),
          ("budget", _bin_budget(a.budget_frac)), ("writes1m", _bin_count(a.writes_1m)), ("tags1m", _bin_count(a.distinct_tags_1m)),
          ("repeat", a.repeat_bin), ("lastread", _bin_age(a.last_read_same_tag_age_s)),
          ("contra", "yes" if a.contradicts_recent_read else "no"), ("err1m", _bin_count(a.errors_1m))]
    p = ctx.plant
    f += [("mode", p.mode.value), ("permit", "yes" if p.permit_open else "no"), ("alarm", "yes" if p.alarm_active else "no")]
    c = ctx.cache
    f += [("servable", "yes" if c.servable else "no"), ("cacheage", _bin_age(c.age_s))]
    for i, h in enumerate(ctx.history[:MAX_HISTORY]):
        same = "same" if h.tag == t.tag else "other"
        f += [(f"h{i}", f"{h.op.value}:{same}:{_bin_age(h.age_s)}:{_bin_delta(h.delta_pct)}:{h.outcome}")]
    return f


def to_text(req: Request, ctx: Context, mode: str = "declared") -> str:
    return " ".join(f"{k}={v}" for k, v in fields(req, ctx, mode))


def to_tokens(req: Request, ctx: Context, mode: str = "declared") -> list[str]:
    out = []
    for k, v in fields(req, ctx, mode):
        if k.startswith("h") and k[1:].isdigit():
            op, same, age, delta, outcome = v.split(":")
            out += [f"H_OP={op}", f"H_TAG={same}", f"H_AGE={age}", f"H_DELTA={delta}", f"H_OUT={outcome}", "H_END"]
        else:
            out.append(f"{k.upper()}={v}")
    return out


def _vocab():
    vals = {
        "OP": ["read", "write", "subscribe", "browse", "call"], "PROTO": ["modbus", "opcua"],
        "CLS": ["setpoint", "command", "tuning", "protective", "interlock", "alarm_limit", "config", "diagnostic", "trap", "unknown"],
        "SAFE": ["down", "up", "none"], "WWR": ["yes", "no"], "COUNT": [f"c{i}" for i in range(6)],
        "ASSET": ["vfd_pump", "vfd_fan", "conveyor", "tank_level_loop", "heater_loop", "reactor_lite", "compressor", "unknown"],
        "STATE": ["running", "stopped", "faulted", "lockout", "maintenance", "unknown"], "NOW": ["present", "absent"],
        "AGE": ["na", "fresh", "recent", "old", "stale"],
        "DELTA": ["na", "0"] + [s + b for s in "+-" for b in ("tiny", "small", "mid", "large", "huge")],
        "LIMITFRAC": ["na", "below", "above", "q0", "q1", "q2", "q3", "q4"],
        "ENV": ["inside_typical", "atypical", "at_limit", "outside", "no_envelope"], "RATE": ["normal", "fast", "shock"],
        "CUM": ["low", "mid", "high"], "DRIFT": ["none", "up", "down", "oscillating", "step"], "DUP": ["yes", "no"],
        "TIER": ["trusted", "normal", "quarantine", "blocked"],
        "ROLE": ["copilot", "optimizer", "dashboard", "historian", "hmi", "engineering", "unknown"],
        "REP": ["bad", "poor", "ok", "good"], "REQS1M": [f"c{i}" for i in range(7)], "BUDGET": ["low", "half", "high", "over", "flood"],
        "WRITES1M": [f"c{i}" for i in range(7)], "TAGS1M": [f"c{i}" for i in range(7)], "REPEAT": ["none", "some", "loop"],
        "LASTREAD": ["na", "fresh", "recent", "old", "stale"], "CONTRA": ["yes", "no"], "ERR1M": [f"c{i}" for i in range(7)],
        "MODE": ["production", "changeover", "maintenance", "startup", "shutdown"], "PERMIT": ["yes", "no"], "ALARM": ["yes", "no"],
        "SERVABLE": ["yes", "no"], "CACHEAGE": ["na", "fresh", "recent", "old", "stale"],
        "H_OP": ["read", "write", "subscribe", "browse", "call"], "H_TAG": ["same", "other"], "H_AGE": ["na", "fresh", "recent", "old", "stale"],
        "H_DELTA": ["na", "0"] + [s + b for s in "+-" for b in ("tiny", "small", "mid", "large", "huge")],
        "H_OUT": ["ok", "error", "denied", "deferred"],
        # register fingerprint (map-free mode); appended after the original vocabulary so existing token ids are stable
        "FP_WRITERS": [f"c{i}" for i in range(7)], "FP_HMI": ["yes", "no"], "FP_W10M": [f"c{i}" for i in range(7)],
        "FP_MOVES": ["yes", "no", "unk"], "FP_LASTWRITE": ["na", "fresh", "recent", "old", "stale", "never"],
        "FP_SEEN_ME": ["yes", "no"], "FP_SEEN_ANY": ["yes", "no"], "FP_RANGE": ["inside", "edge", "far", "none"],
        "FP_DELTA": ["na", "0"] + [s + b for s in "+-" for b in ("tiny", "small", "mid", "large", "huge")],
    }
    toks = ["[PAD]", "[CLS]", "[UNK]", "H_END"]
    toks += [f"[Q_{q}]" for q in ("action", "needed_now", "duplicate", "servable", "over_budget", "contradiction",
                                  "state_conflict", "envelope", "misbehaving", "severity")]
    for k, vs in vals.items():
        toks += [f"{k}={v}" for v in vs]
    return toks


VOCAB: list[str] = _vocab()
TOKEN_ID: dict[str, int] = {t: i for i, t in enumerate(VOCAB)}
QUESTION_IDS = ["action", "needed_now", "duplicate", "servable", "over_budget", "contradiction", "state_conflict",
                "envelope", "misbehaving", "severity"]


def encode(tokens: list[str]) -> list[int]:
    return [TOKEN_ID.get(t, TOKEN_ID["[UNK]"]) for t in tokens]


# student input layout (mirrored by the trainer): [CLS], then one marker token per head in HEADS order, then the rendered tokens.
ACTIONS = ["allow", "cache", "defer", "deny"]
ENVS = ["inside_typical", "atypical", "at_limit", "outside"]
NOULS = ["needed_now", "duplicate", "servable", "over_budget", "contradiction", "state_conflict", "misbehaving"]
HEADS: dict[str, tuple[str, list | None]] = {"action": ("choice", ACTIONS), "envelope": ("choice", ENVS),
                                             "severity": ("score", [0, 1, 2, 3, 4]), **{q: ("noul", None) for q in NOULS}}
MAX_LEN = 160


def pack(tokens: list[str]) -> list[int]:
    ids = [TOKEN_ID["[CLS]"]] + [TOKEN_ID[f"[Q_{q}]"] for q in HEADS] + encode(tokens)
    return ids[:MAX_LEN]


def vocab_meta() -> dict:
    """vocab.json written next to an exported student (what talos/model/onnx.py loads)."""
    return {"tokens": dict(TOKEN_ID), "pad": TOKEN_ID["[PAD]"], "unk": TOKEN_ID["[UNK]"], "cls": TOKEN_ID["[CLS]"],
            "max_len": MAX_LEN, "renderer": "talos/render_v01.py"}


def heads_meta(T: dict | None = None, qhat: float | None = None) -> dict:
    """heads.json: per-head kind + class order (+ temperature, conformal qhat) so the judge can calibrate raw outputs."""
    return {"heads": {q: {"kind": k, "classes": c, "T": float((T or {}).get(q, 1.0))} for q, (k, c) in HEADS.items()},
            "qhat": qhat, "order": list(HEADS)}
