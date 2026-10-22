# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Register fingerprints for the live gate (map-free mode).

What a blind gateway can observe about a register from traffic alone, computed strictly from requests before the current
one. Semantics and binning mirror the training-side fingerprint builder exactly (the lab's parity test asserts it), so
the model sees the same distribution at train and serve time. O(1) per request: sets, counters and a pruned deque.
"""
from __future__ import annotations

from collections import deque

from .schema import Fingerprint, Op

OPERATOR_ROLES = {"hmi", "historian", "engineering"}   # operator-side units, observable through identity, no map needed
WRITE_WINDOW_S = 600.0


class TagObservables:
    __slots__ = (
        "hmi_writes",
        "last_obs",
        "last_write",
        "moves",
        "obs_max",
        "obs_min",
        "touched",
        "write_since_obs",
        "write_ts",
        "writers",
    )

    def __init__(self):
        self.writers: set[str] = set()
        self.touched: set[str] = set()
        self.hmi_writes = False
        self.write_ts: deque = deque()
        self.last_write: tuple[float, float] | None = None
        self.obs_min: float | None = None
        self.obs_max: float | None = None
        self.last_obs: float | None = None
        self.write_since_obs = False
        self.moves = "unk"

    def obs_span(self) -> float | None:
        if self.obs_min is None:
            return None
        return max(self.obs_max - self.obs_min, abs(self.obs_max) * 0.05, 1e-9)

    def obs_range(self) -> tuple[float, float] | None:
        if self.obs_min is None:
            return None
        return (self.obs_min, self.obs_max)

    def fingerprint(self, agent_id: str, now: float, op: str, value: float | None) -> Fingerprint:
        while self.write_ts and self.write_ts[0] < now - WRITE_WINDOW_S:
            self.write_ts.popleft()
        span = self.obs_span()
        rp, dpct = "none", None
        if op == "write" and value is not None and span is not None:
            lo, hi = self.obs_min, self.obs_max
            if lo <= value <= hi:
                rp = "inside"
            elif lo - 0.1 * span <= value <= hi + 0.1 * span:
                rp = "edge"
            else:
                rp = "far"
            if self.last_obs is not None:
                dpct = (value - self.last_obs) / span * 100
        return Fingerprint(writers=len(self.writers), hmi_writes=self.hmi_writes, writes_10m=len(self.write_ts), moves=self.moves,
                           range_pos=rp, last_write_age_s=(now - self.last_write[0]) if self.last_write else None,
                           ever_written=self.last_write is not None, seen_me=agent_id in self.touched, seen_any=bool(self.touched),
                           delta_obs_pct=dpct)

    def touch(self, agent_id: str):
        self.touched.add(agent_id)

    def observe_write(self, agent_id: str | None, role: str | None, ts: float, value: float):
        if agent_id:
            self.writers.add(agent_id)
        if role in OPERATOR_ROLES:
            self.hmi_writes = True
        self.write_ts.append(ts)
        self.last_write = (ts, float(value))
        self.write_since_obs = True
        self._range(value)

    def observe_read(self, value: float):
        value = float(value)
        if self.last_obs is not None and not self.write_since_obs:
            if abs(value - self.last_obs) > 1e-9:
                self.moves = "yes"
            elif self.moves == "unk":
                self.moves = "no"
        self.write_since_obs = False
        self._range(value)

    def _range(self, value: float):
        value = float(value)
        self.last_obs = value
        self.obs_min = value if self.obs_min is None else min(self.obs_min, value)
        self.obs_max = value if self.obs_max is None else max(self.obs_max, value)


class FingerprintStore:
    """tag -> TagObservables. Fed by Gate.decide (who touched what) and the data path (values seen on the wire)."""

    def __init__(self):
        self.tags: dict[str, TagObservables] = {}

    def get(self, tag: str) -> TagObservables:
        t = self.tags.get(tag)
        if t is None:
            t = self.tags[tag] = TagObservables()
        return t

    def fingerprint(self, tag: str, agent_id: str, now: float, op: Op | str, value: float | None) -> Fingerprint:
        o = op.value if isinstance(op, Op) else op
        return self.get(tag).fingerprint(agent_id, now, o, value)

    def obs_range(self, tag: str) -> tuple[float, float] | None:
        t = self.tags.get(tag)
        return t.obs_range() if t else None

    def touch(self, tag: str, agent_id: str):
        self.get(tag).touch(agent_id)

    def observe_write(self, tag: str, value: float, now: float, agent_id: str | None = None, role: str | None = None):
        self.get(tag).observe_write(agent_id, role, now, value)

    def observe_read(self, tag: str, value: float):
        self.get(tag).observe_read(value)
