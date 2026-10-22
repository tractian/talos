# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Adapters: generic I/O lists / tag lists (CSV), Logix tag exports, Ignition tag exports -> TagRow."""
from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterator

from .schema import TagRow, to_float

# column synonyms for CSV-ish exports (lower-case, punctuation stripped)
_COLS = {
    "name": ["tag", "tagname", "tag name", "name", "point", "point name", "tag id", "signal tag", "item"],
    "description": ["description", "desc", "comment", "comments", "service", "label", "tag description", "descr"],
    "unit": ["unit", "units", "uom", "eng unit", "engunit", "engineering unit", "engineering units", "eu"],
    "datatype": ["datatype", "data type", "type", "valuetype", "value type", "dtype"],
    "signal_type": ["signal type", "signal", "io type", "iotype", "i/o type", "io", "kind", "tagtype", "tag type"],
    "address": ["address", "io address", "modbus address", "register", "opcitempath", "opc item path", "item path", "plc address", "node id", "nodeid"],
    "range_lo": ["range lo", "range low", "lo", "low", "min", "minimum", "englow", "eng low", "eng lo", "lrv", "zero"],
    "range_hi": ["range hi", "range high", "hi", "high", "max", "maximum", "enghigh", "eng high", "eng hi", "urv", "span"],
    "alarm_lo": ["alarm lo", "alarm low", "lo alarm", "low alarm", "alarm l", "lo alm"],
    "alarm_hi": ["alarm hi", "alarm high", "hi alarm", "high alarm", "alarm h", "hi alm"],
    "asset": ["asset", "asset id", "equipment", "equipment id", "loop", "device", "machine", "area", "parent", "folder", "path", "panel", "plc panel"],
    "system": ["system", "controller", "plc", "server", "scada", "source"],
    "access": ["access", "readonly", "read only", "externalaccess", "external access", "rw", "writable", "write"],
}
_CANON = {syn: k for k, syns in _COLS.items() for syn in syns}


def _canon(col: str) -> str | None:
    c = re.sub(r"[^a-z0-9/ ]+", " ", col.strip().lower())
    c = re.sub(r"\s+", " ", c).strip()
    return _CANON.get(c)


def _access_from(v: str) -> str:
    v = (v or "").strip().lower()
    if v in ("true", "1", "yes", "ro", "read only", "readonly", "read"):
        return "ro"
    if v in ("false", "0", "no", "rw", "read/write", "readwrite", "read write", "write"):
        return "rw"
    return ""


def csv_rows(path: str, system: str = "") -> Iterator[TagRow]:
    """Generic I/O list / tag export with a header row. Columns are matched by synonym; unknown columns are ignored."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rd = csv.reader(f, dialect)
        header = None
        for raw in rd:
            if header is None:
                if sum(1 for c in raw if _canon(c)) >= 2:
                    header = [_canon(c) for c in raw]
                continue
            d: dict[str, str] = {}
            for k, v in zip(header, raw):
                if k and v and k not in d:
                    d[k] = v.strip()
            if not d.get("name") and not d.get("description"):
                continue
            yield TagRow(name=d.get("name") or d.get("description", ""), description=d.get("description", ""), unit=d.get("unit", ""),
                         datatype=d.get("datatype", ""), signal_type=d.get("signal_type", ""), address=d.get("address", ""),
                         range_lo=to_float(d.get("range_lo")), range_hi=to_float(d.get("range_hi")), alarm_lo=to_float(d.get("alarm_lo")),
                         alarm_hi=to_float(d.get("alarm_hi")), asset=d.get("asset", ""), system=d.get("system") or system,
                         access=_access_from(d.get("access", "")))


def logix_rows(path: str) -> Iterator[TagRow]:
    """RSLogix 5000 / Studio 5000 tag export CSV (TYPE,SCOPE,NAME,DESCRIPTION,DATATYPE,SPECIFIER,ATTRIBUTES)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        header = None
        for raw in rd:
            if header is None:
                if raw and raw[0].strip().upper() == "TYPE":
                    header = [c.strip().upper() for c in raw]
                continue
            d = dict(zip(header, raw))
            if d.get("TYPE", "").upper() not in ("TAG", "ALIAS"):
                continue
            attrs = d.get("ATTRIBUTES", "")
            m = re.search(r"ExternalAccess\s*:=\s*([A-Za-z/ ]+)", attrs)
            access = ""
            if m:
                access = "ro" if "read only" in m.group(1).lower() or m.group(1).strip().lower() == "read" else "rw"
            m = re.search(r"(?:Min|EngMin)\s*:=\s*([-\d.]+)", attrs)
            lo = to_float(m.group(1)) if m else None
            m = re.search(r"(?:Max|EngMax)\s*:=\s*([-\d.]+)", attrs)
            hi = to_float(m.group(1)) if m else None
            name = d.get("NAME", "")
            yield TagRow(name=name, description=d.get("DESCRIPTION", ""), datatype=d.get("DATATYPE", ""), address=d.get("SPECIFIER", ""),
                         range_lo=lo, range_hi=hi, asset=d.get("SCOPE", "") or name.split("_")[0], system="Logix", access=access)


def ignition_rows(path: str) -> Iterator[TagRow]:
    """Ignition tag export: JSON (Tag Browser export) or CSV (legacy)."""
    if path.lower().endswith(".json"):
        with open(path) as f:
            d = json.load(f)

        def walk(node: dict, folder: str):
            tt = node.get("tagType", "")
            name = node.get("name", "")
            if tt in ("Folder", "UdtInstance", "UdtType"):
                for ch in node.get("tags", []):
                    walk(ch, f"{folder}/{name}" if folder else name)
                return
            if tt in ("AtomicTag", "") and name:
                yield TagRow(name=f"{folder}/{name}" if folder else name, description=node.get("documentation", "") or node.get("tooltip", ""),
                             unit=node.get("engUnit", ""), datatype=node.get("dataType", ""), address=node.get("opcItemPath", ""),
                             range_lo=to_float(node.get("engLow")), range_hi=to_float(node.get("engHigh")),
                             alarm_lo=None, alarm_hi=None, asset=folder.split("/")[-1] if folder else "", system="Ignition",
                             access="ro" if node.get("readOnly") else "")

        yield from walk(d if "tags" in d else {"tagType": "Folder", "name": "", "tags": d if isinstance(d, list) else []}, "")
        return
    yield from csv_rows(path, system="Ignition")


ADAPTERS = {"csv": csv_rows, "iolist": csv_rows, "logix": logix_rows, "ignition": ignition_rows}
