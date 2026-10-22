# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Agent identity and per-agent state. See docs/IDENTITY.md.

Pure Python, no I/O, injectable clock (seconds, monotonic)."""
from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass, field

from .schema import Action, Op, Tier

ROLES = ("copilot", "optimizer", "dashboard", "historian", "hmi", "engineering", "unknown")

# requests per minute by tier, per op class ("read" covers read/subscribe/browse, "write" covers write/call)
DEFAULT_BUDGETS: dict[Tier, dict[str, float]] = {
    Tier.trusted: {"read": 600, "write": 120},
    Tier.normal: {"read": 120, "write": 30},
    Tier.quarantine: {"read": 20, "write": 0},
    Tier.blocked: {"read": 0, "write": 0},
}


def op_class(op: Op) -> str:
    return "write" if op in (Op.write, Op.call) else "read"


@dataclass
class TokenBucket:
    rate_per_s: float
    capacity: float
    tokens: float = field(default=None)  # type: ignore
    last: float = 0.0

    def __post_init__(self):
        if self.tokens is None:
            self.tokens = self.capacity

    def _refill(self, now: float):
        if now > self.last:
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate_per_s)
            self.last = now

    def take(self, now: float, n: float = 1.0) -> bool:
        self._refill(now)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def retry_after_s(self, now: float, n: float = 1.0) -> float:
        self._refill(now)
        if self.rate_per_s <= 0:
            return 60.0
        return max(0.0, (n - self.tokens) / self.rate_per_s)

    def frac_used(self) -> float:
        return 1.0 - (self.tokens / self.capacity if self.capacity else 0.0)


@dataclass
class AgentState:
    agent_id: str
    role: str = "unknown"
    tier: Tier = Tier.normal
    registered: bool = False
    rep: float = 0.6                       # EWMA reputation in [0,1]
    buckets: dict[str, TokenBucket] = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=64))   # (ts, op, tag, value, outcome)
    last_read: dict[str, tuple[float, float]] = field(default_factory=dict)    # tag -> (value, ts)
    last_write: dict[str, tuple[float, float]] = field(default_factory=dict)
    bad_hits: deque = field(default_factory=lambda: deque(maxlen=5))   # misbehaving flags for K-of-M hysteresis
    clean_streak: int = 0
    trap_touches: int = 0
    denies: deque = field(default_factory=lambda: deque(maxlen=20))    # timestamps of denies
    tier_entered: float = 0.0
    errors: deque = field(default_factory=lambda: deque(maxlen=64))    # timestamps of error outcomes
    over_budget_ts: deque = field(default_factory=lambda: deque(maxlen=4096))   # timestamps of over-budget defers (flood detection)
    coldstart_hits: dict = field(default_factory=dict)                  # tag -> deferred unfamiliar writes (blind mode); a repeat is a strike

    def set_tier(self, tier: Tier, now: float, budgets: dict[Tier, dict[str, float]] = DEFAULT_BUDGETS):
        if tier != self.tier or not self.buckets:
            self.tier = tier
            self.tier_entered = now
            b = budgets[tier]
            self.buckets = {k: TokenBucket(rate_per_s=v / 60.0, capacity=max(1.0, v / 6.0), last=now) for k, v in b.items()}
            if tier == Tier.quarantine:
                self.clean_streak = 0

    # rolling-window features (1 minute)
    def _recent(self, now: float, window: float = 60.0):
        return [h for h in self.history if now - h[0] <= window]

    def reqs_1m(self, now: float) -> int:
        return len(self._recent(now))

    def writes_1m(self, now: float) -> int:
        return sum(1 for h in self._recent(now) if h[1] in (Op.write, Op.call))

    def distinct_tags_1m(self, now: float) -> int:
        return len({h[2] for h in self._recent(now)})

    def errors_1m(self, now: float) -> int:
        return sum(1 for t in self.errors if now - t <= 60.0)

    def repeat_count_1m(self, now: float, op: Op, tag: str, value: float | None) -> int:
        return sum(1 for h in self._recent(now) if h[1] == op and h[2] == tag and (op != Op.write or h[3] == value))

    def contradicts_recent_read(self, tag: str, value: float | None, now: float, limits=None, window: float = 10.0) -> bool:
        """The agent read this tag moments ago and now writes something far from what it saw, or flips direction
        against its own last write inside the window."""
        if value is None:
            return False
        r = self.last_read.get(tag)
        if r and now - r[1] <= window:
            span = (limits[1] - limits[0]) if limits and limits[1] > limits[0] else max(1.0, abs(r[0]) or 1.0)
            if abs(value - r[0]) > 0.5 * span:
                return True
        w = self.last_write.get(tag)
        if w and now - w[1] <= window and r and now - r[1] <= window:
            if (value - r[0]) * (w[0] - r[0]) < 0 and abs(value - w[0]) > 0:
                return True
        return False


class AgentRegistry:
    def __init__(self, budgets: dict[Tier, dict[str, float]] = DEFAULT_BUDGETS, clock=None):
        self.budgets = budgets
        self.clock = clock or time.monotonic
        self.agents: dict[str, AgentState] = {}
        self.sources: dict[tuple, str] = {}

    def now(self) -> float:
        return self.clock()

    def register(self, agent_id: str, role: str = "unknown", tier: Tier = Tier.normal, now: float | None = None) -> AgentState:
        now = self.now() if now is None else now
        st = self.agents.get(agent_id) or AgentState(agent_id=agent_id)
        st.role = role if role in ROLES else "unknown"
        st.registered = True
        st.set_tier(tier, now, self.budgets)
        self.agents[agent_id] = st
        return st

    def register_source(self, protocol: str, source: dict, agent_id: str):
        self.sources[self._source_key(protocol, source)] = agent_id

    @staticmethod
    def _source_key(protocol: str, source: dict) -> tuple:
        p = str(protocol)
        if p.endswith("modbus"):
            return ("modbus", str(source.get("ip", "")), int(source.get("unit", 0) or 0))
        if p.endswith("opcua"):
            return ("opcua", str(source.get("session_name", "")), str(source.get("app_uri", "")))
        return (p, str(source.get("client_id", source.get("id", ""))))

    def resolve(self, protocol: str, source: dict, now: float | None = None) -> str:
        """Modbus: (ip, unit) -> registered id or u:<hash>. OPC UA: session name 'agent_id|persona|role' or app uri.
        Anything else: client_id. Unknown sources are created in the lowest usable tier (quarantine-sized budget)."""
        now = self.now() if now is None else now
        key = self._source_key(protocol, source)
        if key in self.sources:
            return self.sources[key]
        p = key[0]
        if p == "opcua":
            name = str(source.get("session_name", ""))
            if "|" in name:
                aid, *rest = name.split("|")
                role = rest[1] if len(rest) > 1 else "unknown"
                if aid not in self.agents:
                    self.register(aid, role=role, tier=Tier.normal, now=now)
                self.sources[key] = aid
                return aid
        elif p not in ("modbus",):
            cid = key[1]
            if cid:
                if cid not in self.agents:
                    self.register(cid, now=now)
                self.sources[key] = cid
                return cid
        h = hashlib.sha256(repr(key).encode()).hexdigest()[:8]
        aid = f"u:{h}"
        if aid not in self.agents:
            st = AgentState(agent_id=aid, registered=False)
            st.set_tier(Tier.normal, now, self._unknown_budgets())
            self.agents[aid] = st
        self.sources[key] = aid
        return aid

    def _unknown_budgets(self) -> dict[Tier, dict[str, float]]:
        """Unregistered sources get quarantine-sized read budget and a tiny write budget, but normal-tier rules."""
        b = {k: dict(v) for k, v in self.budgets.items()}
        b[Tier.normal] = {"read": self.budgets[Tier.quarantine]["read"], "write": max(1.0, self.budgets[Tier.normal]["write"] / 10)}
        return b

    def state(self, agent_id: str, now: float | None = None) -> AgentState:
        now = self.now() if now is None else now
        st = self.agents.get(agent_id)
        if st is None:
            st = AgentState(agent_id=agent_id)
            st.set_tier(Tier.normal, now, self.budgets)
            self.agents[agent_id] = st
        return st

    def note_request(self, agent_id: str, op: Op, tag: str, value: float | None, now: float):
        self.state(agent_id, now).history.appendleft((now, op, tag, value, "pending"))

    def note_outcome(self, agent_id: str, op: Op, tag: str, value: float | None, action: Action, now: float,
                     observed_value: float | None = None, trap: bool = False, error: bool = False):
        st = self.state(agent_id, now)
        if st.history and st.history[0][4] == "pending":
            ts, o, t, v, _ = st.history[0]
            st.history[0] = (ts, o, t, v, action.value if not error else "error")
        if error:
            st.errors.appendleft(now)
        if action == Action.deny:
            st.denies.appendleft(now)
        if op in (Op.read, Op.subscribe) and observed_value is not None and action in (Action.allow, Action.cache):
            st.last_read[tag] = (float(observed_value), now)
        if op == Op.write and action == Action.allow and value is not None:
            st.last_write[tag] = (float(value), now)
        # reputation EWMA
        delta = {Action.allow: +0.02, Action.cache: +0.01, Action.defer: -0.01, Action.deny: -0.06}[action]
        if trap:
            delta = -0.25
            st.trap_touches += 1
        if error:
            delta -= 0.02
        st.rep = min(1.0, max(0.0, 0.9 * st.rep + 0.1 * (st.rep + delta * 10)))
        if action in (Action.allow, Action.cache) and not trap and not error:
            st.clean_streak += 1
        else:
            st.clean_streak = 0


def rep_bin(rep: float) -> str:
    if rep < 0.25:
        return "bad"
    if rep < 0.45:
        return "poor"
    if rep < 0.7:
        return "ok"
    return "good"
