# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Normalized tag rows (what adapters emit) and `talos.map/1` builders (`talos.mapformat` owns the schema)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from talos.mapformat import SCHEMA, validate

KINDS = ("hr", "ir", "co", "di")
CLASSES = ("setpoint", "command", "tuning", "protective", "interlock", "alarm_limit", "config", "diagnostic")
SAFE = ("down", "up", "none")


def name_hash(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()[:10]


@dataclass
class TagRow:
    """What an adapter extracts from one export line. Free text stays here and never reaches a plant map."""
    name: str
    description: str = ""
    unit: str = ""
    datatype: str = ""
    signal_type: str = ""          # AI/AO/DI/DO/... as the export says it
    address: str = ""              # as exported (40001, %MW12, Local:1:I.Data.3, ns=2;s=...)
    range_lo: float | None = None
    range_hi: float | None = None
    alarm_lo: float | None = None
    alarm_hi: float | None = None
    asset: str = ""                # asset / loop / equipment the export groups it under
    system: str = ""
    access: str = ""               # rw/ro/wo when the export says so

    @property
    def text(self) -> str:
        return " ".join(x for x in (self.name, self.description) if x)


def register(address: int, kind: str, name: str, cls: str, safe_direction: str, wwr: bool, *, access="rw",
             param="", unit="", scale=1.0, lo=None, hi=None, default=None, envelope=None, role=None, exposed=True,
             classified_by="rule", source="import", extra: dict | None = None) -> dict:
    assert kind in KINDS and cls in CLASSES and safe_direction in SAFE, (kind, cls, safe_direction)
    if (lo is None) != (hi is None) or (lo is not None and lo > hi):
        lo = hi = None
    r = {"address": int(address), "kind": kind, "name": name, "param": param, "unit": unit or "", "scale": float(scale or 1.0),
         "min": lo, "max": hi, "default": default, "access": access, "cls": cls, "safe_direction": safe_direction,
         "writable_while_running": bool(wwr), "envelope": envelope, "role": role, "exposed": bool(exposed),
         "classified_by": classified_by, "source": source}
    if extra:
        r.update(extra)
    return r


def make_map(device: dict, registers: list[dict], layer: str = "plant") -> dict:
    """Assemble + validate a talos.map/1 file. Plant maps must only carry hashed names (caller's duty)."""
    dev = {"vendor": "", "family": "", "product": "", "protocol": "modbus_tcp", "identity": {}, "address_formula": "",
           "speed_unit": "Hz", "speed_max": 60.0, "sources": []}
    dev.update(device)
    roles = {r["role"]: r["address"] for r in registers if r.get("role")}
    m = {"schema": SCHEMA, "layer": layer, "device": dev, "roles": roles, "registers": registers}
    validate(m)
    if layer == "plant":
        for r in registers:
            assert re.fullmatch(r"[0-9a-f]{10}", r["name"]), "plant maps carry hashed names only"
    return m


# ---- address parsing ---------------------------------------------------------------------------------------------
_MODICON = re.compile(r"^\s*([0134])(\d{4,5})\s*$")            # 40001 / 400001 / 30001 / 00001 / 10001
_PREFIXED = re.compile(r"^\s*(hr|ir|co|di|holding|input|coil|discrete)[:\s#]*(\d+)\s*$", re.I)
_IEC = re.compile(r"^\s*%([MIQ])([XWDB]?)(\d+)(?:\.(\d+))?\s*$", re.I)    # %MW12, %IX0.3, %QX1.2
_FX = re.compile(r"^\s*([0134])x[:\s]*(\d+)\s*$", re.I)                   # 4x:12, 3x12


def parse_address(addr, hint_kind: str = "") -> tuple[int | None, str]:
    """(0-based wire address, kind) from the notations exports use. Unknown -> (None, hint or 'hr')."""
    if addr is None:
        return None, hint_kind or "hr"
    s = str(addr).strip()
    if not s:
        return None, hint_kind or "hr"
    m = _MODICON.match(s)
    if m:
        lead, num = m.group(1), int(m.group(2))
        return max(0, num - 1), {"4": "hr", "3": "ir", "0": "co", "1": "di"}[lead]
    m = _FX.match(s)
    if m:
        return int(m.group(2)), {"4": "hr", "3": "ir", "0": "co", "1": "di"}[m.group(1)]
    m = _PREFIXED.match(s)
    if m:
        k = m.group(1).lower()
        return int(m.group(2)), {"holding": "hr", "input": "ir", "coil": "co", "discrete": "di"}.get(k, k)
    m = _IEC.match(s)
    if m:
        area, size, word, bit = m.group(1).upper(), m.group(2).upper(), int(m.group(3)), m.group(4)
        if size == "X" or (bit is not None and size == ""):
            return word * 8 + int(bit or 0), {"M": "co", "I": "di", "Q": "co"}[area]
        return word, {"M": "hr", "I": "ir", "Q": "hr"}[area]
    if s.isdigit():
        return int(s), hint_kind or "hr"
    return None, hint_kind or "hr"


def guess_scale(unit: str, lo: float | None, hi: float | None, datatype: str = "") -> float:
    """raw = eng * scale so the engineering range fits a uint16 register."""
    dt = (datatype or "").lower()
    if dt in ("bool", "boolean", "bit", "digital") or unit in ("bit", "bool", "Boolean"):
        return 1.0
    if lo is None or hi is None or hi <= lo:
        return {"Hz": 100.0, "s": 10.0, "%": 10.0, "C": 10.0, "degC": 10.0, "°C": 10.0, "bar": 100.0, "A": 10.0}.get(unit, 1.0)
    span = max(abs(hi), abs(lo))
    for scale in (100.0, 10.0, 1.0):
        if span * scale <= 65535:
            return scale
    return 1.0


def to_float(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(str(x).replace(",", "."))
    except ValueError:
        return None
    if v != v:
        return None
    return v
