# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Tag rows -> plant-layer `talos.map/1`.

An export (I/O list, Logix or Ignition tag export) becomes a list of TagRow; each row is classified (rules first,
LLM for the uncertain ones), given a register kind / scale / envelope, and written out as a hashed-name register.
Assets are grouped by the export's equipment id, else the ISA loop number, else the first name token, and each
group gets a coarse equipment kind by keyword vote (used for role hints and reporting only).
"""
from __future__ import annotations

import re

from .classify import LLM_THRESHOLD, Verdict, classify
from .schema import TagRow, guess_scale, name_hash, parse_address, register

EQUIPMENT_KINDS = ("vfd_pump", "vfd_fan", "compressor", "conveyor", "tank_level_loop", "heater_loop", "reactor_lite")

# keyword votes for an asset's equipment kind
KIND_WORDS = {
    "vfd_pump": r"\b(pump|pmp|p-?\d{2,}|booster|injection|transfer|dosing|metering|lift station|well)\b",
    "vfd_fan": r"\b(fan|blower|draft|id fan|fd fan|pa fan|exhaust|ventil|rtu|ahu|supply air|return air|cooling tower|damper)\b",
    "compressor": r"\b(compressor|cmp|comp|cylinder|recip|screw|stage|air dryer|receiver)\b",
    "conveyor": r"\b(conveyor|belt|hoist|winder|unwind|rewind|line speed|footage|coil|shiploader|boom|tripper|feeder|extruder|capstan|payoff|take-?up)\b",
    "tank_level_loop": r"\b(tank|level|sump|clarifier|reservoir|basin|silo|hopper|vessel level|lt-?\d|lic-?\d|wt\d|water tank)\b",
    "heater_loop": r"\b(heater|heating|boiler|furnace|oven|steam|attemp|superheat|economi[sz]er|exchanger|hx|jacket temp|tic-?\d|burner|kiln|dryer)\b",
    "reactor_lite": r"\b(reactor|pressure vessel|autoclave|digester|fermenter|batch|vent|pic-?\d|pshh|separator|scrubber|absorber)\b",
}
_KIND_RX = {k: re.compile(v, re.I) for k, v in KIND_WORDS.items()}
_ISA = re.compile(r"\b([A-Z]{1,4})[-_ ]?(\d{2,5})([A-Z]?)\b")


def _norm(s: str) -> str:
    return re.sub(r"[_\.\-:/\\\[\]\(\)@#]+", " ", s)


def group_key(row: TagRow) -> str:
    """Asset the row belongs to: the export's asset if any, else the ISA loop number, else the first name token."""
    if row.asset:
        return row.asset
    m = _ISA.search(row.name)
    if m:
        return m.group(2)
    return _norm(row.name).split()[0] if row.name.strip() else "misc"


def vote_kind(rows: list[TagRow], key: str) -> tuple[str | None, float]:
    text = " ".join(_norm(f"{key} {r.name} {r.description}") for r in rows)
    votes = {k: len(rx.findall(text)) for k, rx in _KIND_RX.items()}
    total = sum(votes.values())
    if total == 0:
        return None, 0.0
    k, n = max(votes.items(), key=lambda kv: kv[1])
    return k, n / total


def _kind_for(v: Verdict, row: TagRow) -> str:
    boolish = (row.datatype or "").lower().startswith("bool") or (row.unit or "").lower() in ("bit", "bool", "boolean") or \
        (row.range_lo == 0 and row.range_hi == 1) or (row.signal_type or "").upper() in ("DI", "DO")
    if v.cls == "interlock" and boolish:
        return "co" if v.writable else "di"
    if boolish and not v.writable:
        return "di" if (row.signal_type or "").upper() == "DI" else "ir"
    return "hr" if v.writable else "ir"


def tag_spec(row: TagRow, v: Verdict) -> dict:
    kind = _kind_for(v, row)
    lo, hi = row.range_lo, row.range_hi
    if lo is not None and hi is not None and hi <= lo:
        lo = hi = None
    if kind in ("co", "di"):
        lo, hi = 0.0, 1.0
    scale = guess_scale(row.unit, lo, hi, row.datatype)
    if scale == 1.0 and lo is not None and hi is not None and (hi - lo) < 10 and kind == "hr":
        scale = 100.0
    env = None
    if lo is not None and hi is not None:
        if row.alarm_lo is not None and row.alarm_hi is not None and lo <= row.alarm_lo < row.alarm_hi <= hi:
            env = [row.alarm_lo, row.alarm_hi]
        elif v.cls in ("setpoint", "diagnostic") and kind in ("hr", "ir"):
            env = [lo + 0.3 * (hi - lo), lo + 0.8 * (hi - lo)]
    default = 0.0
    if lo is not None and hi is not None and v.cls in ("protective", "alarm_limit", "tuning", "setpoint"):
        default = lo + (0.85 if v.cls in ("protective", "alarm_limit") else 0.5) * (hi - lo)
    fast = v.cls in ("setpoint", "command", "diagnostic", "interlock")
    limits = [lo, hi] if (lo is not None and hi is not None) else None
    if v.writable and limits is None and kind == "hr":
        limits = [0.0, 1.0] if v.cls == "command" else [0.0, 100.0]
    return {"name_hash": name_hash(row.name), "cls": v.cls, "kind": kind, "unit": (row.unit or ("bit" if kind in ("co", "di") else ""))[:12],
            "limits": limits, "envelope": env, "writable": v.writable, "wwr": v.wwr and v.writable, "safe": v.safe_direction,
            "scale": scale, "default": round(float(default), 4), "fast": fast, "classified_by": v.by, "confidence": v.confidence}


def classify_rows(rows: list[TagRow], llm=None) -> list[Verdict]:
    """Rules first; rows under LLM_THRESHOLD go to the LLM when one is given."""
    verdicts = [classify(r) for r in rows]
    if llm is None:
        return verdicts
    from .llm import merge
    ask = [r for r, v in zip(rows, verdicts) if v.confidence < LLM_THRESHOLD]
    answers = llm.classify_many(ask) if ask else {}
    return [merge(v, answers.get(llm.key(r))) if v.confidence < LLM_THRESHOLD else v for r, v in zip(rows, verdicts)]


def build_plant_map(rows: list[TagRow], llm=None, device: dict | None = None) -> tuple[dict, list[dict]]:
    """Plant-layer talos.map/1 from an export: hashed names, addresses parsed from the export where possible,
    synthetic sequential addresses otherwise (reported in the second return value)."""
    verdicts = classify_rows(rows, llm)
    regs, seen, unaddressed = [], set(), []
    next_addr = {"hr": 40000, "ir": 30000, "co": 0, "di": 10000}
    for r, v in zip(rows, verdicts):
        spec = tag_spec(r, v)
        kind = spec["kind"]
        addr, pk = parse_address(r.address, kind)
        if addr is not None and pk in ("hr", "ir", "co", "di"):
            kind = pk
            if (kind, addr) in seen:
                addr = None
        if addr is None:
            unaddressed.append(r.name)
            while (kind, next_addr[kind]) in seen:
                next_addr[kind] += 1
            addr = next_addr[kind]
            next_addr[kind] += 1
        seen.add((kind, addr))
        lo, hi = (spec["limits"] or [None, None])
        regs.append(register(addr, kind, spec["name_hash"], v.cls, v.safe_direction, spec["wwr"], access="rw" if v.writable else "ro",
                             param="", unit=spec["unit"], scale=spec["scale"], lo=lo, hi=hi, default=spec["default"], envelope=spec["envelope"],
                             role=None, exposed=True, classified_by=v.by, source="import",
                             extra={"confidence": v.confidence, "synthetic_address": r.name in unaddressed[-1:] and addr >= 0 and not parse_address(r.address)[0] is not None}))
    from .schema import make_map
    dev = {"vendor": "", "family": "plant", "product": "", "protocol": "modbus_tcp", "address_formula": "as exported; synthetic 4xxxx/3xxxx when the export has none"}
    dev.update(device or {})
    return make_map(dev, regs, layer="plant"), unaddressed
