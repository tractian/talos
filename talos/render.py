# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Build the typed Context and render it. Two renderings from the same record: compact closed-vocabulary text
for Kev/Jev-style judges, and symbol tokens for the student. No free strings ever (docs/INJECTION.md)."""
from __future__ import annotations

from .cache import ValueCache
from .fastpath import FastpathResult
from .identity import AgentState, rep_bin
from .schema import (
    QUESTIONS,
    AgentContext,
    AssetState,
    CacheContext,
    Context,
    Fingerprint,
    HistoryRow,
    Op,
    PlantContext,
    Request,
    TargetNow,
    WriteFeatures,
)
from .world import PlantState, TagInfo, World

HISTORY_ROWS = 8


def _bin_delta(d: float | None) -> str:
    if d is None:
        return "na"
    a = abs(d)
    s = "+" if d > 0 else ("-" if d < 0 else "")
    if a == 0:
        return "zero"
    if a < 1:
        return s + "tiny"
    if a < 5:
        return s + "small"
    if a < 20:
        return s + "medium"
    if a < 50:
        return s + "large"
    return s + "huge"


def _bin_frac(f: float | None) -> str:
    if f is None:
        return "na"
    if f < 0.1:
        return "bottom"
    if f < 0.35:
        return "low"
    if f < 0.65:
        return "mid"
    if f < 0.9:
        return "high"
    return "top"


def _bin_age(a: float | None) -> str:
    if a is None:
        return "na"
    if a < 1:
        return "fresh"
    if a < 5:
        return "recent"
    if a < 60:
        return "aging"
    return "stale"


def _bin_count(n: int, budget_per_min: float) -> str:
    if budget_per_min <= 0:
        return "high" if n else "low"
    f = n / budget_per_min
    return "low" if f < 0.3 else ("mid" if f < 0.8 else "high")


def _bin_budget(f: float) -> str:
    return "low" if f < 0.5 else ("mid" if f < 0.9 else "exhausted")


def build_context(request: Request, agent: AgentState, world: World | None, plant: PlantState,
                  cache: ValueCache, fp: FastpathResult, now: float, budget_per_min: float = 120.0,
                  fingerprint: Fingerprint | None = None) -> Context:
    tag: TagInfo | None = world.tags.get(request.target.tag) if world else None
    asset = tag.asset if tag else "unknown"
    asset_kind = (world.assets[asset].kind if world and asset in world.assets else "unknown")
    if asset_kind not in ("vfd_pump", "vfd_fan", "conveyor", "tank_level_loop", "heater_loop", "reactor_lite", "compressor"):
        asset_kind = "unknown"
    asset_state = plant.assets.get(asset, AssetState.unknown)
    tv = plant.tags.get(request.target.tag)
    cv = cache.peek(request.target.tag, now)
    if tv is not None:
        value, age = tv.value, tv.age_s
    elif cv is not None:
        value, age = cv
    else:
        value, age = None, None
    fresh = cache.fresh(request.target.tag, now)
    repeat = agent.repeat_count_1m(now, request.op, request.target.tag, request.value)
    lr = agent.last_read.get(request.target.tag)
    hist = []
    for ts, op, t, v, outcome in list(agent.history)[1:1 + HISTORY_ROWS]:   # [0] is the pending current request
        row = HistoryRow(op=op, tag=t, age_s=round(now - ts, 1),
                         outcome={"allow": "ok", "cache": "ok", "pending": "ok", "error": "error", "denied": "denied",
                                  "deny": "denied", "defer": "deferred", "deferred": "deferred"}.get(outcome, "ok"))
        hist.append(row)
    return Context(
        asset_kind=asset_kind,
        asset_state=asset_state,
        target_now=TargetNow(value=value, age_s=age, limits=tag.limits if tag else None, envelope=tag.envelope if tag else None),
        write=WriteFeatures(delta_pct=fp.delta_pct, limit_frac=fp.limit_frac, envelope_pos=fp.envelope_pos,
                            implied_rate=fp.implied_rate, cum_delta_bin=fp.cum_delta_bin, drift=fp.drift, duplicate=fp.duplicate),
        agent=AgentContext(tier=agent.tier, role=agent.role, rep_bin=rep_bin(agent.rep), reqs_1m=agent.reqs_1m(now),
                           budget_frac=fp.budget_frac, writes_1m=agent.writes_1m(now), distinct_tags_1m=agent.distinct_tags_1m(now),
                           repeat_bin="none" if repeat <= 1 else ("some" if repeat < 6 else "loop"),
                           last_read_same_tag_age_s=(round(now - lr[1], 1) if lr else None),
                           contradicts_recent_read=agent.contradicts_recent_read(request.target.tag, request.value, now, tag.limits if tag else None),
                           errors_1m=agent.errors_1m(now)),
        plant=PlantContext(mode=plant.mode, permit_open=plant.permit_open, alarm_active=plant.alarm_active),
        history=hist,
        cache=CacheContext(servable=bool(fresh) and request.op in (Op.read, Op.subscribe), age_s=(fresh[1] if fresh else (cv[1] if cv else None)),
                           ttl_s=cache.ttl(request.target.tag)),
        fingerprint=fingerprint,
    )


def _pairs(request: Request, ctx: Context, budget_per_min: float = 120.0) -> list[tuple[str, str]]:
    a, w, p, c = ctx.agent, ctx.write, ctx.plant, ctx.cache
    out = [
        ("op", request.op.value), ("proto", request.protocol.value), ("cls", request.target.cls.value),
        ("count", "one" if request.count <= 1 else ("few" if request.count <= 8 else "many")),
        ("asset", ctx.asset_kind), ("state", ctx.asset_state.value),
        ("safe_dir", request.target.safe_direction.value),
        ("cur", "known" if ctx.target_now.value is not None else "unknown"), ("cur_age", _bin_age(ctx.target_now.age_s)),
    ]
    if request.op == Op.write:
        out += [("delta", _bin_delta(w.delta_pct)), ("limit_pos", _bin_frac(w.limit_frac)), ("env", w.envelope_pos),
                ("rate", w.implied_rate), ("cum", w.cum_delta_bin), ("drift", w.drift), ("dup", "yes" if w.duplicate else "no"),
                ("contra", "yes" if a.contradicts_recent_read else "no")]
    out += [("tier", a.tier.value), ("role", a.role), ("rep", a.rep_bin), ("reqs", _bin_count(a.reqs_1m, budget_per_min)),
            ("budget", _bin_budget(a.budget_frac)), ("writes", "none" if a.writes_1m == 0 else ("few" if a.writes_1m < 5 else "many")),
            ("tags", "one" if a.distinct_tags_1m <= 1 else ("few" if a.distinct_tags_1m <= 6 else "many")),
            ("repeat", a.repeat_bin), ("last_read", _bin_age(a.last_read_same_tag_age_s)),
            ("errors", "none" if a.errors_1m == 0 else ("some" if a.errors_1m < 5 else "many")),
            ("mode", p.mode.value), ("permit", "open" if p.permit_open else "none"), ("alarm", "yes" if p.alarm_active else "no"),
            ("cache", "fresh" if c.servable else ("stale" if c.age_s is not None else "none"))]
    return out


def to_text(request: Request, ctx: Context) -> str:
    lines = [" ".join(f"{k}={v}" for k, v in _pairs(request, ctx))]
    for h in ctx.history:
        lines.append(f"hist op={h.op.value} same_tag={'yes' if h.tag == request.target.tag else 'no'} age={_bin_age(h.age_s)} delta={_bin_delta(h.delta_pct)} out={h.outcome}")
    return "\n".join(lines)


def to_tokens(request: Request, ctx: Context) -> list[str]:
    toks = [f"[{k.upper()}={v}]" for k, v in _pairs(request, ctx)]
    for h in ctx.history:
        toks += ["[HIST]", f"[HOP={h.op.value}]", f"[HSAME={'yes' if h.tag == request.target.tag else 'no'}]",
                 f"[HAGE={_bin_age(h.age_s)}]", f"[HOUT={h.outcome}]"]
    return toks


def to_systemone_body(request: Request, ctx: Context, model: str = "talos") -> dict:
    return {"model": model, "state": to_text(request, ctx), "questions": QUESTIONS}
