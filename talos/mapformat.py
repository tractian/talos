# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""The `talos.map/1` register-map format: constants and the validator.

A map describes one device (vendor layer) or one plant (plant layer) as a list of registers with a class,
safe direction, limits and access. The importers (`talos.importers`) build maps; the gate consumes the same
fields through `talos.world`. Loading a map library is a separate concern and lives outside this module.
"""
from __future__ import annotations

from .schema import RegClass, SafeDirection

SCHEMA = "talos.map/1"
KINDS = ("hr", "ir", "co", "di")
ACCESS = ("rw", "ro", "wo")
CLASSIFIED_BY = ("rule", "llm", "binding")


def validate(m: dict) -> None:
    """Raise ValueError on the first structural problem; return None when `m` is a well-formed talos.map/1."""
    if m.get("schema") != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA}")
    dev = m["device"]
    for k in ("vendor", "family", "protocol", "speed_unit", "speed_max"):
        if k not in dev:
            raise ValueError(f"device.{k} missing")
    seen: set[tuple[str, int]] = set()
    roles = m.get("roles", {})
    by_addr = {}
    for r in m["registers"]:
        key = (r["kind"], int(r["address"]))
        if r["kind"] not in KINDS:
            raise ValueError(f"bad kind {r['kind']} at {r.get('param')}")
        if key in seen:
            raise ValueError(f"duplicate address {key}")
        seen.add(key)
        by_addr[key] = r
        if r["access"] not in ACCESS:
            raise ValueError(f"bad access {r['access']} at {r.get('param')}")
        RegClass(r["cls"])
        SafeDirection(r["safe_direction"])
        if r["classified_by"] not in CLASSIFIED_BY:
            raise ValueError(f"bad classified_by at {r.get('param')}")
        if not r.get("name"):
            raise ValueError(f"register at {key} has no name")
        if (r.get("min") is None) != (r.get("max") is None):
            raise ValueError(f"min/max must both be set or both null at {r.get('param')}")
        if r.get("min") is not None and r["min"] > r["max"]:
            raise ValueError(f"min > max at {r.get('param')}")
        if float(r.get("scale") or 0) <= 0:
            raise ValueError(f"scale must be > 0 at {r.get('param')}")
        if r.get("role") and roles.get(r["role"]) != r["address"]:
            raise ValueError(f"role {r['role']} not bound to {r['address']} in roles")
        if r.get("bits") is not None:
            b = r["bits"]
            if "run" not in b or "mask" not in b or "run_word" not in b or "stop_word" not in b:
                raise ValueError(f"bits incomplete at {r.get('param')}")
    for role, addr in roles.items():
        hits = [r for r in m["registers"] if r["address"] == addr and r.get("role") == role]
        if len(hits) != 1:
            raise ValueError(f"role {role} -> {addr} does not resolve to exactly one register")
