# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Policy: a threshold table over calibrated answers + fastpath flags -> Action, reasons, tier transitions.
The decision enum is a table, not a trained head, so an operating point moves without retraining.
Invariant: the model can only make the outcome stricter than the fastpath allowed."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .fastpath import FastpathResult
from .identity import AgentState
from .schema import Action, Op, Request, Tier

STRICTNESS = {Action.allow: 0, Action.cache: 1, Action.defer: 2, Action.deny: 3}


def stricter(a: Action, b: Action) -> Action:
    return a if STRICTNESS[a] >= STRICTNESS[b] else b


@dataclass
class PolicyConfig:
    # per-request thresholds
    deny_p: float = 0.80            # p(action=deny)
    deny_severity: float = 3.0      # expected severity
    deny_state_conflict: float = 0.90
    defer_p: float = 0.50
    defer_over_budget: float = 0.80
    defer_misbehaving: float = 0.70
    defer_contradiction: float = 0.85
    cache_servable: float = 0.80
    cache_duplicate: float = 0.90
    abstain_max_p: float = 0.45     # if no action probability reaches this, abstain -> defer (quarantine band)
    trusted_deny_p: float = 0.95    # trusted tier: deny only when very sure
    # tier hysteresis
    misbehaving_hit: float = 0.70
    quarantine_k: int = 3
    quarantine_m: int = 5
    quarantine_exit_clean: int = 20
    quarantine_exit_min_s: float = 120.0
    block_on_denies: int = 8         # denies inside quarantine within deny_window_s -> blocked
    deny_window_s: float = 300.0
    trusted_after_clean: int = 500   # normal -> trusted after this many consecutive clean requests (0 = manual only)
    defer_retry_ms: int = 500
    quarantine_retry_ms: int = 5000
    # flood: an agent the fastpath keeps deferring for budget is misbehaving even if the model never sees a request
    flood_window_s: float = 30.0
    flood_over_budget: int = 300     # over-budget defers inside flood_window_s -> quarantine (0 = off)

    @classmethod
    def load(cls, path: str | Path) -> PolicyConfig:
        d = yaml.safe_load(Path(path).read_text()) or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def dump(self) -> dict:
        return asdict(self)


@dataclass
class PolicyOutcome:
    action: Action
    reasons: list[str] = field(default_factory=list)
    retry_after_ms: int | None = None
    tier_before: Tier = Tier.normal
    tier_after: Tier = Tier.normal


def _p(ans: dict, qid: str, key: str | None = None, default: float = 0.0) -> float:
    a = ans.get(qid)
    if a is None:
        return default
    if key is None:
        return float(a) if not isinstance(a, dict) else float(a.get("score", default) or default)
    return float(a.get(key, default)) if isinstance(a, dict) else default


class Policy:
    def __init__(self, cfg: PolicyConfig | None = None):
        self.cfg = cfg or PolicyConfig()

    def decide(self, request: Request, agent: AgentState, fp: FastpathResult, answers: dict | None,
               safe_direction: bool, stop_bypass: bool, now: float) -> PolicyOutcome:
        cfg = self.cfg
        out = PolicyOutcome(action=Action.allow, tier_before=agent.tier, tier_after=agent.tier)
        floor = fp.action or Action.allow     # what the fastpath already decided (None = allow unless the model objects)
        if fp.over_budget:
            agent.over_budget_ts.append(now)
        if "coldstart:unfamiliar_write" in fp.reasons:
            n = agent.coldstart_hits.get(request.target.tag, 0) + 1
            agent.coldstart_hits[request.target.tag] = n
            if n >= 2:   # first unfamiliar write is held quietly; insisting on it is a strike
                agent.bad_hits.append(True)
                out.reasons.append("coldstart:repeat")
        model_action = Action.allow
        if answers:
            pa = answers.get("action") or {}
            p_deny, p_defer, p_cache = float(pa.get("deny", 0)), float(pa.get("defer", 0)), float(pa.get("cache", 0))
            sev = _p(answers, "severity")
            deny_thr = cfg.trusted_deny_p if agent.tier == Tier.trusted else cfg.deny_p
            if p_deny >= deny_thr or sev >= cfg.deny_severity or _p(answers, "state_conflict") >= cfg.deny_state_conflict:
                model_action = Action.deny
                out.reasons.append(f"model:deny p={p_deny:.2f} sev={sev:.1f}")
            elif (p_defer >= cfg.defer_p or _p(answers, "over_budget") >= cfg.defer_over_budget
                  or _p(answers, "misbehaving") >= cfg.defer_misbehaving
                  or (request.op == Op.write and _p(answers, "contradiction") >= cfg.defer_contradiction)):
                model_action = Action.defer
                out.reasons.append(f"model:defer p={p_defer:.2f} mis={_p(answers, 'misbehaving'):.2f}")
            elif (request.op in (Op.read, Op.subscribe) and _p(answers, "servable") >= cfg.cache_servable) or \
                 (request.op == Op.write and _p(answers, "duplicate") >= cfg.cache_duplicate) or p_cache >= cfg.defer_p:
                model_action = Action.cache
                out.reasons.append("model:cache")
            elif pa and max(pa.values()) < cfg.abstain_max_p:
                model_action = Action.defer
                out.reasons.append("model:abstain")
            # tier hysteresis bookkeeping
            agent.bad_hits.append(_p(answers, "misbehaving") >= cfg.misbehaving_hit)
        action = stricter(floor, model_action)
        if fp.reasons:
            out.reasons = [f"fastpath:{r}" for r in fp.reasons] + out.reasons

        # safe-direction protection: with stop-bypass off, a safe-direction write is only deniable/deferrable for quarantined agents
        if safe_direction and request.op == Op.write and not stop_bypass and action in (Action.deny, Action.defer):
            if agent.tier not in (Tier.quarantine, Tier.blocked):
                action = Action.allow
                out.reasons.append("safe_direction:protected")
        if action == Action.defer:
            out.retry_after_ms = fp.retry_after_ms or (cfg.quarantine_retry_ms if agent.tier == Tier.quarantine else cfg.defer_retry_ms)
        out.action = action
        out.tier_after = self.update_tier(agent, action, now)
        return out

    def update_tier(self, agent: AgentState, action: Action, now: float, trap: bool = False) -> Tier:
        cfg = self.cfg
        t = agent.tier
        if t == Tier.blocked:
            return t
        if trap:
            new = Tier.blocked if (t == Tier.quarantine or agent.trap_touches >= 2) else Tier.quarantine
            agent.set_tier(new, now)
            return new
        if t == Tier.quarantine:
            recent_denies = sum(1 for d in agent.denies if now - d <= cfg.deny_window_s)
            if recent_denies >= cfg.block_on_denies:
                agent.set_tier(Tier.blocked, now); return Tier.blocked
            if agent.clean_streak >= cfg.quarantine_exit_clean and now - agent.tier_entered >= cfg.quarantine_exit_min_s:
                agent.set_tier(Tier.normal, now); return Tier.normal
            return t
        hits = sum(1 for h in agent.bad_hits if h)
        if len(agent.bad_hits) >= cfg.quarantine_m and hits >= cfg.quarantine_k:
            agent.set_tier(Tier.quarantine, now); agent.bad_hits.clear(); return Tier.quarantine
        if cfg.flood_over_budget and len(agent.over_budget_ts) >= cfg.flood_over_budget \
                and now - agent.over_budget_ts[-cfg.flood_over_budget] <= cfg.flood_window_s:
            agent.set_tier(Tier.quarantine, now); agent.over_budget_ts.clear(); return Tier.quarantine
        if t == Tier.normal and cfg.trusted_after_clean and agent.registered and agent.clean_streak >= cfg.trusted_after_clean:
            agent.set_tier(Tier.trusted, now); return Tier.trusted
        return t
