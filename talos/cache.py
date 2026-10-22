# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Value cache (serve fresh reads without touching the controller) and decision cache (replay identical
decisions within a TTL)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import orjson


@dataclass
class Cached:
    value: float
    ts: float


class ValueCache:
    def __init__(self, default_ttl_s: float = 2.0):
        self.default_ttl_s = default_ttl_s
        self.values: dict[str, Cached] = {}
        self.ttls: dict[str, float] = {}

    def set_ttl(self, tag: str, ttl_s: float):
        self.ttls[tag] = ttl_s

    def put(self, tag: str, value: float, now: float):
        self.values[tag] = Cached(float(value), now)

    def peek(self, tag: str, now: float) -> tuple[float, float] | None:
        """-> (value, age_s) regardless of freshness, or None."""
        c = self.values.get(tag)
        return (c.value, now - c.ts) if c else None

    def fresh(self, tag: str, now: float) -> tuple[float, float] | None:
        """-> (value, age_s) if inside the tag's TTL, else None."""
        c = self.values.get(tag)
        if not c:
            return None
        age = now - c.ts
        return (c.value, age) if age <= self.ttls.get(tag, self.default_ttl_s) else None

    def ttl(self, tag: str) -> float:
        return self.ttls.get(tag, self.default_ttl_s)


def canonical_request(req: Any) -> str:
    d = req.model_dump(mode="json") if hasattr(req, "model_dump") else dict(req)
    d.pop("ts_rel", None)
    return orjson.dumps(d, option=orjson.OPT_SORT_KEYS).decode()


def state_hash(ctx: Any) -> str:
    """Coarse hash of the parts of the context that change the verdict; per-request counters and ages are left out
    so that identical requests in the same plant state replay within the TTL."""
    d = ctx.model_dump(mode="json") if hasattr(ctx, "model_dump") else dict(ctx)
    a = d.get("agent", {})
    keep = {
        "asset_kind": d.get("asset_kind"), "asset_state": d.get("asset_state"),
        "value": (d.get("target_now") or {}).get("value"),
        "write": d.get("write"), "plant": d.get("plant"),
        "tier": a.get("tier"), "rep": a.get("rep_bin"), "contra": a.get("contradicts_recent_read"),
        "servable": (d.get("cache") or {}).get("servable"),
    }
    return hashlib.sha256(orjson.dumps(keep, option=orjson.OPT_SORT_KEYS)).hexdigest()[:16]


class DecisionCache:
    def __init__(self, ttl_s: float = 2.0, maxsize: int = 10000):
        self.ttl_s = ttl_s
        self.maxsize = maxsize
        self.items: dict[tuple, tuple[float, Any]] = {}

    def key(self, agent_id: str, req: Any, ctx: Any) -> tuple:
        return (agent_id, canonical_request(req), state_hash(ctx))

    def get(self, key: tuple, now: float):
        it = self.items.get(key)
        if it and now - it[0] <= self.ttl_s:
            return it[1]
        if it:
            self.items.pop(key, None)
        return None

    def put(self, key: tuple, decision: Any, now: float):
        if len(self.items) >= self.maxsize:
            # drop the oldest third
            for k in sorted(self.items, key=lambda k: self.items[k][0])[: self.maxsize // 3]:
                self.items.pop(k, None)
        self.items[key] = (now, decision)
