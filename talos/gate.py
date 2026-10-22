# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""The Gate: identity -> traps -> fastpath -> cache -> render -> model -> policy -> ledger. See docs/PIPELINE.md.

Hard rules enforced here:
  * the model can only make the outcome stricter than the fastpath allowed;
  * a model failure/timeout never yields allow (fastpath verdict, else defer; reads fall back to cache when fresh);
  * safe-direction writes are not deniable for non-quarantined agents when stop_bypass is on (they bypass the model),
    and when it is off they are only deniable for quarantine/blocked tiers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import render_v01
from .cache import DecisionCache, ValueCache
from .fastpath import Fastpath, FastpathConfig, FastpathResult, is_safe_direction
from .fingerprints import FingerprintStore
from .identity import DEFAULT_BUDGETS, AgentRegistry
from .ledger import Ledger
from .model import Judge, JudgeError, JudgeTimeout, NullJudge
from .policy import Policy, PolicyConfig
from .render import build_context, to_systemone_body, to_text, to_tokens
from .schema import Action, AssetState, Decision, Op, Request, Tier
from .traps import TrapSet
from .world import PlantState, World


@dataclass
class GateConfig:
    stop_bypass: bool = True
    model_deadline_ms: float = 20.0
    on_model_failure: str = "defer"            # "defer" (default; reads from cache if fresh) | "fastpath_only" (degraded mode, flagged)
    decision_cache_ttl_s: float = 2.0
    value_ttl_s: float = 2.0
    trusted_reads_skip_model: bool = False  # opt-in: trusted-tier reads skip the model. Off by default (D19): humans go through the
                                            # same judge as agents (fat-finger check for free, no lane to hijack); right of way = budget + toward-safe bypass
    map_mode: str = "auto"                  # auto | declared | declared_fp | fingerprint  (auto: declared_fp for mapped tags, fingerprint for unmapped)
    shadow: bool = False
    fastpath: FastpathConfig = field(default_factory=FastpathConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    budgets: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_BUDGETS.items()})


class Gate:
    def __init__(self, config: GateConfig | None = None, world: World | None = None, judge: Judge | None = None,
                 ledger: Ledger | None = None, registry: AgentRegistry | None = None, clock=None):
        self.cfg = config or GateConfig()
        self.cfg.fastpath.stop_bypass = self.cfg.stop_bypass
        self.world = world or World()
        self.judge = judge or NullJudge()
        self.ledger = ledger or Ledger(None)
        self.clock = clock or time.monotonic
        self.registry = registry or AgentRegistry(budgets=self.cfg.budgets, clock=self.clock)
        self.traps = TrapSet.from_world(self.world)
        self.fastpath = Fastpath(self.cfg.fastpath)
        self.values = ValueCache(self.cfg.value_ttl_s)
        for t in self.world.tags.values():
            self.values.set_ttl(t.tag, t.ttl_s)
        self.decisions = DecisionCache(self.cfg.decision_cache_ttl_s)
        self.fingerprints = FingerprintStore()     # what a blind gateway learns about each register from traffic (map-free mode)
        self.blind = not self.world.tags           # no map at all: every tag is judged from its fingerprint
        self.policy = Policy(self.cfg.policy)
        self.stats = {"requests": 0, "by": {}, "actions": {}, "model_failures": 0}

    # observations from the data path (the proxy calls these)
    def observe_read(self, tag: str, value: float, now: float | None = None):
        self.values.put(tag, value, self.clock() if now is None else now)
        self.fingerprints.observe_read(tag, value)

    def observe_write(self, tag: str, value: float, now: float | None = None, agent_id: str | None = None):
        now = self.clock() if now is None else now
        self.values.put(tag, value, now)
        self.fastpath.note_write(tag, value, now)
        role = self.registry.agents[agent_id].role if agent_id and agent_id in self.registry.agents else None
        self.fingerprints.observe_write(tag, value, now, agent_id=agent_id, role=role)

    def render_mode(self, tag_known: bool) -> str:
        m = self.cfg.map_mode
        if m != "auto":
            return m
        if not tag_known:
            return "fingerprint"
        return "declared_fp" if getattr(self.judge, "supports_fp", True) else "declared"

    def _fill_target(self, request: Request):
        t = self.world.tags.get(request.target.tag)
        if t:
            request.target.cls = t.cls
            request.target.writable_while_running = t.writable_while_running
            request.target.safe_direction = t.safe_direction
            request.target.is_trap = request.target.is_trap or t.is_trap

    def decide(self, request: Request, plant_state: PlantState | dict | None = None, source: dict | None = None,
               now: float | None = None) -> Decision:
        """Synchronous decision: the model (if needed) is called inline."""
        gen = self._decide_gen(request, plant_state, source, now)
        try:
            body, deadline = next(gen)
        except StopIteration as done:
            return done.value
        try:
            res = (self.judge.judge(body, deadline_s=deadline), None)
        except JudgeError as e:
            res = (None, e)
        except Exception as e:  # noqa: BLE001 - a crashing judge is a model failure, never an exception to the caller
            res = (None, JudgeError(f"{type(e).__name__}: {e}"))
        try:
            gen.send(res)
        except StopIteration as done:
            return done.value
        raise RuntimeError("gate: decision generator did not finish")

    async def decide_async(self, request: Request, plant_state: PlantState | dict | None = None, source: dict | None = None,
                           now: float | None = None) -> Decision:
        """Same decision, but the model call runs in a worker thread so the event loop (and every other connection,
        e.g. the HMI's reads) keeps moving while the model thinks. All gate state is touched on the loop thread."""
        import asyncio
        gen = self._decide_gen(request, plant_state, source, now)
        try:
            body, deadline = next(gen)
        except StopIteration as done:
            return done.value
        try:
            answers = await asyncio.wait_for(asyncio.to_thread(self.judge.judge, body, deadline), timeout=deadline + 0.005)
            res = (answers, None)
        except JudgeError as e:
            res = (None, e)
        except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 - separate classes before Python 3.11
            res = (None, JudgeTimeout(f"deadline {deadline:.3f}s"))
        except Exception as e:  # noqa: BLE001 - a crashing judge is a model failure, never a dropped client
            res = (None, JudgeError(f"{type(e).__name__}: {e}"))
        try:
            gen.send(res)
        except StopIteration as done:
            return done.value
        raise RuntimeError("gate: decision generator did not finish")

    def _decide_gen(self, request: Request, plant_state: PlantState | dict | None = None, source: dict | None = None,
                    now: float | None = None):
        t0 = time.perf_counter()
        now = self.clock() if now is None else now
        plant = plant_state if isinstance(plant_state, PlantState) else PlantState.from_dict(plant_state)
        if source is not None:
            request.agent_id = self.registry.resolve(request.protocol.value, source, now)
        agent = self.registry.state(request.agent_id, now)
        self._fill_target(request)
        tag = self.world.tags.get(request.target.tag)
        tv = plant.tags.get(request.target.tag)
        cv = self.values.peek(request.target.tag, now)
        current = tv.value if tv is not None else (cv[0] if cv else None)
        if tv is not None:
            self.values.put(request.target.tag, tv.value, now - tv.age_s)
        asset_state = plant.assets.get(tag.asset, AssetState.unknown) if tag else AssetState.unknown
        self.registry.note_request(request.agent_id, request.op, request.target.tag, request.value, now)
        safe = is_safe_direction(request, tag, current)
        # register fingerprint from traffic strictly before this request (map-free mode); then mark the touch
        fingerprint = self.fingerprints.fingerprint(request.target.tag, request.agent_id, now, request.op, request.value)
        blind_tag = tag is None
        self.fingerprints.touch(request.target.tag, request.agent_id)
        mode = self.render_mode(tag_known=not blind_tag)

        by = "policy"; answers = None; text = None; fp = FastpathResult(); outcome = None; cached = None
        # 2. traps
        if self.traps.is_trap(request):
            action = Action.deny; by = "traps"
            self.registry.note_outcome(request.agent_id, request.op, request.target.tag, request.value, Action.deny, now, trap=True)
            tier_after = self.policy.update_tier(agent, Action.deny, now, trap=True)
            reasons = ["trap"]
        else:
            # 3. fastpath
            fp = self.fastpath.evaluate(request, agent, tag, current, asset_state, now,
                                        current_fresh=self.values.fresh(request.target.tag, now) is not None,
                                        fingerprint=fingerprint, blind=self.blind,   # cold start only when the gate has no map at all;
                                        obs_range=self.fingerprints.obs_range(request.target.tag) if blind_tag else None)   # on a mapped plant an unmapped write stays unknown_target
            ctx = build_context(request, agent, self.world, plant, self.values, fp, now,
                                budget_per_min=self.cfg.budgets.get(agent.tier, {}).get("read", 120.0), fingerprint=fingerprint)
            text = to_text(request, ctx)
            if fp.action is not None:
                by = "fastpath"
                outcome = self.policy.decide(request, agent, fp, None, safe, self.cfg.stop_bypass, now)
                action, reasons, tier_after = outcome.action, outcome.reasons, outcome.tier_after
            else:
                # 4. cache
                if request.op in (Op.read, Op.subscribe) and ctx.cache.servable:
                    action = Action.cache; by = "cache"; reasons = ["cache:fresh"]
                    outcome = None; tier_after = agent.tier
                elif agent.tier == Tier.trusted and request.op in (Op.read, Op.subscribe) and self.cfg.trusted_reads_skip_model:
                    # fast lane: a trusted HMI/historian read never waits behind the model (the fastpath already budgeted it)
                    action = Action.allow; by = "fastpath"; reasons = ["trusted:read"]
                    outcome = None; tier_after = agent.tier
                else:
                    key = self.decisions.key(request.agent_id, request, ctx)
                    cached = self.decisions.get(key, now)
                    if cached is not None:
                        by = "cache"; action = cached["action"]; reasons = list(cached["reasons"]) + ["cache:decision"]
                        answers = cached["answers"]; tier_after = agent.tier
                    else:
                        # 5. render + model
                        body = to_systemone_body(request, ctx)
                        body["state"] = render_v01.to_text(request, ctx, mode)   # the teacher is trained on the render_v01 text, not the ledger text
                        body["render_mode"] = mode
                        # the ONNX student was trained on the render_v01 symbol tokens; other judges get the live renderer's
                        body["tokens"] = (render_v01.to_tokens(request, ctx, mode) if getattr(self.judge, "wants", None) == "render_v01"
                                          else to_tokens(request, ctx))
                        answers, err = yield body, self.cfg.model_deadline_ms / 1000.0   # the caller runs the model (inline or in a thread)
                        if err is None:
                            by = "model"
                        else:
                            self.stats["model_failures"] += 1
                            answers = None
                            by = "fallback"
                            fp.reasons.append(f"model_failure:{type(err).__name__}")
                        if by == "fallback":
                            if fp.action is not None:
                                action = fp.action
                            elif self.cfg.on_model_failure == "fastpath_only":
                                action = Action.allow; fp.reasons.append("degraded:fastpath_only")
                            elif request.op in (Op.read, Op.subscribe) and ctx.cache.age_s is not None:
                                action = Action.cache
                            else:
                                action = Action.defer
                            outcome = self.policy.decide(request, agent, FastpathResult(action=action, reasons=fp.reasons,
                                                                                         retry_after_ms=fp.retry_after_ms or 500),
                                                         None, safe, self.cfg.stop_bypass, now)
                        else:
                            outcome = self.policy.decide(request, agent, fp, answers, safe, self.cfg.stop_bypass, now)
                        action, reasons, tier_after = outcome.action, outcome.reasons, outcome.tier_after
            self.registry.note_outcome(request.agent_id, request.op, request.target.tag, request.value, action, now,
                                       observed_value=(current if request.op in (Op.read, Op.subscribe) else None))
            if cached is None and by == "model":
                self.decisions.put(self.decisions.key(request.agent_id, request, ctx),
                                   {"action": action, "reasons": list(reasons), "answers": answers}, now)

        p = {}
        if answers and isinstance(answers.get("action"), dict):
            p = {k: round(float(v), 4) for k, v in answers["action"].items()}
        latency_ms = (time.perf_counter() - t0) * 1000.0
        dec = Decision(action=action, by=by, p=p, answers=self._flat(answers), tier=tier_after, reasons=reasons,
                       retry_after_ms=(outcome.retry_after_ms if outcome else (fp.retry_after_ms if action == Action.defer else None)),
                       latency_ms=round(latency_ms, 3))
        if self.cfg.shadow:
            dec.reasons = [f"shadow:would={action.value}"] + dec.reasons
            dec.action = Action.allow
        row = {"agent": request.agent_id, "op": request.op.value, "tag": request.target.tag, "cls": request.target.cls.value,
               "value": request.value, "action": action.value, "returned": dec.action.value, "by": by, "tier": tier_after.value,
               "reasons": reasons, "answers": dec.answers, "state": text, "latency_ms": dec.latency_ms, "shadow": self.cfg.shadow}
        dec.ledger_id = self.ledger.append(row)
        self.stats["requests"] += 1
        self.stats["by"][by] = self.stats["by"].get(by, 0) + 1
        self.stats["actions"][dec.action.value] = self.stats["actions"].get(dec.action.value, 0) + 1
        return dec

    @staticmethod
    def _flat(answers: dict | None) -> dict:
        if not answers:
            return {}
        out = {}
        for k, v in answers.items():
            if isinstance(v, dict):
                if "score" in v:
                    out[k] = v["score"]
                else:
                    out[k] = max(v, key=v.get) if v else None
                    for kk, vv in v.items():
                        out[f"{k}.{kk}"] = round(float(vv), 4)
            else:
                out[k] = round(float(v), 4) if v is not None else None
        return out
