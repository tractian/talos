# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Talos schema. One request, one typed context, one fixed question set. See docs/SCHEMA.md.

Everything the model sees is built from these types. No free text crosses into the model: tags, agents and
assets are interned ids; numbers are binned to closed-vocabulary words by the renderer.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Protocol(str, Enum):
    modbus = "modbus"
    opcua = "opcua"


class Op(str, Enum):
    read = "read"
    write = "write"
    subscribe = "subscribe"
    browse = "browse"
    call = "call"


class RegClass(str, Enum):
    setpoint = "setpoint"
    command = "command"
    tuning = "tuning"
    protective = "protective"
    interlock = "interlock"
    alarm_limit = "alarm_limit"
    config = "config"
    diagnostic = "diagnostic"
    trap = "trap"
    unknown = "unknown"


class SafeDirection(str, Enum):
    down = "down"      # toward stopped / zero / closed
    up = "up"
    none = "none"      # no safe direction (e.g. config)


class AssetState(str, Enum):
    running = "running"
    stopped = "stopped"
    faulted = "faulted"
    lockout = "lockout"
    maintenance = "maintenance"
    unknown = "unknown"


class PlantMode(str, Enum):
    production = "production"
    changeover = "changeover"
    maintenance = "maintenance"
    startup = "startup"
    shutdown = "shutdown"


class Tier(str, Enum):
    trusted = "trusted"
    normal = "normal"
    quarantine = "quarantine"
    blocked = "blocked"


class Action(str, Enum):
    allow = "allow"
    cache = "cache"
    defer = "defer"
    deny = "deny"


class Target(BaseModel):
    tag: str                                  # interned id, e.g. "t:4821"
    cls: RegClass = RegClass.unknown
    writable_while_running: bool = True
    safe_direction: SafeDirection = SafeDirection.none
    is_trap: bool = False


class Request(BaseModel):
    agent_id: str                             # interned id, e.g. "a:7f3c" or "u:<hash>" for unregistered
    protocol: Protocol
    op: Op
    target: Target
    value: float | None = None             # raw value for writes
    count: int = 1                            # registers / nodes touched
    budget_charge: bool = True                # False for the 2nd..nth register of one wire frame: one frame, one token
    ts_rel: float = 0.0                       # seconds relative to episode start; absolute time never leaves the gateway


class TargetNow(BaseModel):
    value: float | None = None
    age_s: float | None = None
    limits: tuple[float, float] | None = None       # hard limits from the asset profile
    envelope: tuple[float, float] | None = None     # learned typical band


EnvelopePos = Literal["inside_typical", "atypical", "at_limit", "outside", "no_envelope"]
Bin3 = Literal["low", "mid", "high"]
Drift = Literal["none", "up", "down", "oscillating", "step"]


class WriteFeatures(BaseModel):
    delta_pct: float | None = None
    limit_frac: float | None = None        # 0..1 position inside hard limits
    envelope_pos: EnvelopePos = "no_envelope"
    implied_rate: Literal["normal", "fast", "shock"] = "normal"
    cum_delta_bin: Bin3 = "low"               # cumulative move on this register over the window
    drift: Drift = "none"
    duplicate: bool = False                   # value already equal to target_now.value


class AgentContext(BaseModel):
    tier: Tier = Tier.normal
    role: Literal["copilot", "optimizer", "dashboard", "historian", "hmi", "engineering", "unknown"] = "unknown"
    rep_bin: Literal["bad", "poor", "ok", "good"] = "ok"
    reqs_1m: int = 0
    budget_frac: float = 0.0                  # 0..1+ of the agent's per-minute budget used
    writes_1m: int = 0
    distinct_tags_1m: int = 0
    repeat_bin: Literal["none", "some", "loop"] = "none"
    last_read_same_tag_age_s: float | None = None
    contradicts_recent_read: bool = False
    errors_1m: int = 0


class PlantContext(BaseModel):
    mode: PlantMode = PlantMode.production
    permit_open: bool = False
    alarm_active: bool = False


class HistoryRow(BaseModel):
    op: Op
    tag: str
    age_s: float
    delta_pct: float | None = None
    outcome: Literal["ok", "error", "denied", "deferred"] = "ok"


class CacheContext(BaseModel):
    servable: bool = False
    age_s: float | None = None
    ttl_s: float | None = None


class Fingerprint(BaseModel):
    """What a blind gateway can observe about a register from traffic alone (map-free mode, docs/PLAN.md zero-config lane).
    Everything is computed from requests strictly before the current one."""
    writers: int = 0                          # distinct agents that have written this register
    hmi_writes: bool = False                  # an operator-side unit (hmi / historian / engineering role) has written it
    writes_10m: int = 0                       # writes by anyone in the last 10 min
    moves: Literal["yes", "no", "unk"] = "unk"   # value changed between observations with no write in between
    range_pos: Literal["inside", "edge", "far", "none"] = "none"   # requested value vs the observed range (writes only)
    last_write_age_s: float | None = None  # None = never written
    ever_written: bool = False
    seen_me: bool = False                     # this agent has touched this register before
    seen_any: bool = False                    # anyone has (False = never-touched, trap-like)
    delta_obs_pct: float | None = None     # write delta vs the last known value, % of the OBSERVED range


class Context(BaseModel):
    asset_kind: Literal["vfd_pump", "vfd_fan", "conveyor", "tank_level_loop", "heater_loop", "reactor_lite", "compressor", "unknown"] = "unknown"
    asset_state: AssetState = AssetState.unknown
    target_now: TargetNow = Field(default_factory=TargetNow)
    write: WriteFeatures = Field(default_factory=WriteFeatures)
    agent: AgentContext = Field(default_factory=AgentContext)
    plant: PlantContext = Field(default_factory=PlantContext)
    history: list[HistoryRow] = Field(default_factory=list)   # bounded, most recent first
    cache: CacheContext = Field(default_factory=CacheContext)
    fingerprint: Fingerprint | None = None                   # map-free observables; None = not computed


class Decision(BaseModel):
    action: Action
    by: Literal["traps", "fastpath", "cache", "model", "policy", "fallback"]
    p: dict[str, float] = Field(default_factory=dict)        # calibrated probabilities over actions
    answers: dict[str, float | str] = Field(default_factory=dict)  # per-question answers (noul p, choice key, score)
    tier: Tier = Tier.normal
    reasons: list[str] = Field(default_factory=list)
    retry_after_ms: int | None = None
    ledger_id: str | None = None
    latency_ms: float | None = None


# The fixed question set (v0.1). Keys are stable; the policy table and the training records both use them.
QUESTIONS: dict[str, dict] = {
    "action": {"type": "choice", "instructions": "What should the gate do with this request?",
               "criteria": {"allow": "Forward it to the controller now",
                            "cache": "Answer from the cached value without touching the controller",
                            "defer": "Hold it and let the agent retry later",
                            "deny": "Refuse it"}},
    "needed_now": {"type": "noul", "instructions": "The controller must be touched now to satisfy this request."},
    "duplicate": {"type": "noul", "instructions": "The write sets a value the target already holds."},
    "servable": {"type": "noul", "instructions": "This read can be answered from the cached value."},
    "over_budget": {"type": "noul", "instructions": "The agent is exceeding a reasonable request rate for its role."},
    "contradiction": {"type": "noul", "instructions": "The write contradicts what the agent read moments ago."},
    "state_conflict": {"type": "noul", "instructions": "The machine state does not permit this request."},
    "envelope": {"type": "choice", "instructions": "Where does the written value sit relative to the asset's normal range?",
                 "criteria": {"inside_typical": "Inside the typical band", "atypical": "Inside limits but unusual",
                              "at_limit": "At a hard limit", "outside": "Outside hard limits"}},
    "misbehaving": {"type": "noul", "instructions": "The agent's recent behaviour is that of a faulty or hostile client."},
    "severity": {"type": "score", "instructions": "How bad would it be to forward this request as-is?",
                 "criteria": ["none", "wasteful", "disruptive", "harmful", "dangerous"]},
}
