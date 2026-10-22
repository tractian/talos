# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Deterministic fast path. Runs before the model, answers the obvious cases, and computes the flags the
renderer needs. See docs/PIPELINE.md step 3. Everything here is explainable in one sentence."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .identity import AgentState, op_class
from .schema import Action, AssetState, Fingerprint, Op, RegClass, Request, SafeDirection, Tier
from .world import TagInfo

# which register classes each tier may write. Everything not listed is a deny for that tier.
DEFAULT_WRITE_CLASSES: dict[Tier, set[RegClass]] = {
    Tier.trusted: {RegClass.setpoint, RegClass.command, RegClass.tuning, RegClass.protective, RegClass.interlock,
                   RegClass.alarm_limit, RegClass.config, RegClass.diagnostic},
    Tier.normal: {RegClass.setpoint, RegClass.command, RegClass.tuning, RegClass.diagnostic},
    Tier.quarantine: set(),
    Tier.blocked: set(),
}
READ_OPS = {Op.read, Op.subscribe, Op.browse}


@dataclass
class FastpathConfig:
    stop_bypass: bool = True
    write_classes: dict[Tier, set[RegClass]] = field(default_factory=lambda: {k: set(v) for k, v in DEFAULT_WRITE_CLASSES.items()})
    allow_unknown_reads: bool = True          # reads of unmapped addresses pass (flagged); writes to them never do (mapped plant)
    coldstart: bool = True                    # blind gate (no map): a write to a never-written register is deferred + flagged, never denied by this rule
    cum_delta_window_s: float = 600.0
    cum_delta_cap_frac: float = 0.6           # cumulative move over the window above this fraction of the limit range = deny
    drift_min_steps: int = 5                  # consecutive same-sign writes to call it a drift
    shock_delta_frac: float = 0.5             # single write moving more than this fraction of the range = "shock"
    running_write_check: bool = True          # deny writes to tags not writable while the asset runs
    duplicate_ack: bool = True                # a write equal to a fresh current value is acked from cache


@dataclass
class FastpathResult:
    action: Action | None = None           # final verdict, or None = let the model decide
    reasons: list[str] = field(default_factory=list)
    bypass: bool = False                      # stop-bypass fired (safe-direction write, no model)
    retry_after_ms: int | None = None
    # flags for the renderer / policy
    over_budget: bool = False
    budget_frac: float = 0.0
    delta_pct: float | None = None
    limit_frac: float | None = None
    envelope_pos: str = "no_envelope"
    implied_rate: str = "normal"
    cum_delta_bin: str = "low"
    drift: str = "none"
    duplicate: bool = False
    state_conflict: bool = False
    unknown_target: bool = False


class RegisterAccumulators:
    """Per-register write history for cumulative-delta and drift detection (the arithmetic the model cannot do)."""

    def __init__(self, window_s: float = 600.0, maxlen: int = 64):
        self.window_s = window_s
        self.hist: dict[str, deque] = {}
        self.maxlen = maxlen

    def note(self, tag: str, value: float, now: float):
        d = self.hist.setdefault(tag, deque(maxlen=self.maxlen))
        d.append((now, float(value)))

    def features(self, tag: str, proposed: float, now: float, limits, min_steps: int = 5) -> tuple[float, str]:
        """-> (cumulative move as a fraction of the limit range over the window, drift label) including the proposed write."""
        d = [(t, v) for (t, v) in self.hist.get(tag, ()) if now - t <= self.window_s]
        vals = [v for _, v in d] + [float(proposed)]
        span = (limits[1] - limits[0]) if limits and limits[1] > limits[0] else None
        if len(vals) < 2:
            return 0.0, "none"
        cum = abs(vals[-1] - vals[0]) / span if span else 0.0
        deltas = [b - a for a, b in zip(vals[:-1], vals[1:]) if b != a]
        if not deltas:
            return cum, "none"
        signs = [1 if x > 0 else -1 for x in deltas]
        # trailing run of same sign
        run = 1
        for i in range(len(signs) - 1, 0, -1):
            if signs[i] == signs[i - 1]:
                run += 1
            else:
                break
        if run >= min_steps:
            return cum, "up" if signs[-1] > 0 else "down"
        # alternating signs
        alt = 1
        for i in range(len(signs) - 1, 0, -1):
            if signs[i] != signs[i - 1]:
                alt += 1
            else:
                break
        if alt >= 4:
            return cum, "oscillating"
        if span and abs(deltas[-1]) / span >= 0.25:
            return cum, "step"
        return cum, "none"


def envelope_position(value: float, limits, envelope) -> str:
    if limits and (value < limits[0] or value > limits[1]):
        return "outside"
    if limits and (value == limits[0] or value == limits[1]):
        return "at_limit"
    if envelope:
        return "inside_typical" if envelope[0] <= value <= envelope[1] else "atypical"
    return "no_envelope"


def is_safe_direction(request: Request, tag: TagInfo | None, current: float | None) -> bool:
    """A write that moves the asset toward its safe state (stop, zero, closed)."""
    if request.op != Op.write or request.value is None:
        return False
    if tag is not None and tag.stop_values and float(request.value) in tag.stop_values:
        return True   # a declared stop word (e.g. 0x0300 on a fieldbus control register)
    sd = tag.safe_direction if tag else request.target.safe_direction
    if sd == SafeDirection.none:
        return False
    if current is None:
        # without a current value, only a write to the limit in the safe direction counts
        lim = tag.limits if tag else None
        if not lim:
            return False
        return request.value <= lim[0] if sd == SafeDirection.down else request.value >= lim[1]
    return request.value < current if sd == SafeDirection.down else request.value > current


class Fastpath:
    def __init__(self, config: FastpathConfig | None = None):
        self.cfg = config or FastpathConfig()
        self.acc = RegisterAccumulators(window_s=self.cfg.cum_delta_window_s)

    def note_write(self, tag: str, value: float, now: float):
        self.acc.note(tag, value, now)

    def evaluate(self, request: Request, agent: AgentState, tag: TagInfo | None, current: float | None,
                 asset_state: AssetState, now: float, current_fresh: bool = False,
                 fingerprint: Fingerprint | None = None, blind: bool = False,
                 obs_range: tuple[float, float] | None = None) -> FastpathResult:
        """blind = the gate has no map for this tag (no world, or unmapped address): register semantics come from the
        fingerprint, write features are measured against the OBSERVED range (obs_range) instead of declared limits."""
        r = FastpathResult()
        cfg = self.cfg
        tier = agent.tier
        cls = tag.cls if tag else request.target.cls
        limits = tag.limits if tag else None
        envelope = tag.envelope if tag else None
        if blind and limits is None and obs_range and obs_range[1] > obs_range[0]:
            limits = obs_range            # observed span stands in for the limit span in delta/cum/drift arithmetic (builder does the same)
            envelope = None

        # 0. blocked tier: nothing passes
        if tier == Tier.blocked:
            r.action = Action.deny; r.reasons.append("tier:blocked"); return r

        # 1. unknown function / service
        if request.op == Op.call and (tag is None or cls == RegClass.unknown):
            r.action = Action.deny; r.reasons.append("unknown_function"); return r

        # 2. unknown target
        if tag is None or cls == RegClass.unknown:
            r.unknown_target = True
            if request.op in READ_OPS:
                if not cfg.allow_unknown_reads:
                    r.action = Action.deny; r.reasons.append("unknown_target"); return r
                if blind:
                    r.reasons.append("blind")
            elif blind and cfg.coldstart:
                # cold start (docs/PLAN.md zero-config lane): a write to a register nobody has ever written is held, not refused;
                # the agent gets a strike only if it repeats it (policy). Registers with write history go on to the model.
                if fingerprint is None or not fingerprint.ever_written:
                    r.action = Action.defer; r.reasons.append("coldstart:unfamiliar_write"); r.retry_after_ms = 5000; return r
                r.reasons.append("blind")
            else:
                r.action = Action.deny; r.reasons.append("unknown_target"); return r

        # 3. write features (before allowlist so the renderer always has them)
        if request.op == Op.write and request.value is not None:
            v = float(request.value)
            if current is not None:
                base = (limits[1] - limits[0]) if limits and limits[1] > limits[0] else (abs(current) or 1.0)
                r.delta_pct = round(100.0 * (v - current) / base, 2)
                r.duplicate = (v == current)
                if abs(v - current) / base >= cfg.shock_delta_frac:
                    r.implied_rate = "shock"
                elif abs(v - current) / base >= 0.2:
                    r.implied_rate = "fast"
            if limits and limits[1] > limits[0]:
                r.limit_frac = round((v - limits[0]) / (limits[1] - limits[0]), 3)
            r.envelope_pos = envelope_position(v, limits, envelope)
            cum, drift = self.acc.features(request.target.tag, v, now, limits, cfg.drift_min_steps)
            r.cum_delta_bin = "low" if cum < 0.2 else ("mid" if cum < cfg.cum_delta_cap_frac else "high")
            r.drift = drift

        safe = is_safe_direction(request, tag, current)

        # 4. stop-bypass: safe-direction writes pass without a model call (all tiers but blocked)
        if request.op == Op.write and safe and cfg.stop_bypass:
            if not blind and limits and request.value is not None and (request.value < limits[0] or request.value > limits[1]):
                pass  # out-of-limits still clamped below
            else:
                r.action = Action.allow; r.bypass = True; r.reasons.append("stop_bypass"); return r

        # 5. tier allowlist for writes
        if request.op in (Op.write, Op.call):
            if tier == Tier.quarantine:
                if safe:   # stop_bypass off: safe-direction writes from quarantine go to the model (deniable there)
                    pass
                else:
                    r.action = Action.defer; r.reasons.append("quarantine:read_only"); r.retry_after_ms = 5000; return r
            elif cls != RegClass.unknown and cls not in cfg.write_classes.get(tier, set()) and not safe:
                r.action = Action.deny; r.reasons.append(f"allowlist:{tier.value}:{cls.value}"); return r   # only when the class is known

        # 6. hard clamp (declared limits only; an observed range is evidence for the model, not a clamp)
        if request.op == Op.write and request.value is not None and not blind and limits and (request.value < limits[0] or request.value > limits[1]):
            r.action = Action.deny; r.reasons.append("hard_clamp"); return r

        # 7. writable-while-running / state conflict (deterministic part)
        if request.op == Op.write and tag is not None and cfg.running_write_check:
            if not tag.writable_while_running and asset_state == AssetState.running and not safe:
                r.state_conflict = True
                r.action = Action.deny; r.reasons.append("not_writable_while_running"); return r
            if asset_state == AssetState.lockout and not safe:
                r.state_conflict = True
                r.action = Action.deny; r.reasons.append("lockout"); return r

        # 8. rate budget
        bucket = agent.buckets.get(op_class(request.op))
        if bucket is not None:
            r.budget_frac = round(bucket.frac_used(), 3)
            if not bucket.take(now, 1.0):   # per request, not per register: a block poll is one round trip
                r.over_budget = True
                r.retry_after_ms = int(1000 * bucket.retry_after_s(now)) + 50
                if not safe:
                    r.action = Action.defer; r.reasons.append("over_budget"); return r

        # 9. cumulative-delta hard cap
        if r.cum_delta_bin == "high" and not safe:
            r.action = Action.deny; r.reasons.append("cum_delta_cap"); return r

        # 10. exact duplicate of a fresh current value: acknowledge without touching the controller
        if request.op == Op.write and r.duplicate and current_fresh and cfg.duplicate_ack:
            r.action = Action.cache; r.reasons.append("duplicate_write"); return r

        return r
