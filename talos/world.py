# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""World (tag map) and live PlantState. The world is the gateway's static knowledge of a plant: which addresses
are which tags, their class, limits, envelope, safe direction, traps. PlantState is what the gateway observed
most recently. Loaded from the sim's world.json; a real gateway would build the same shape from its tag database."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .schema import AssetState, PlantMode, RegClass, SafeDirection


@dataclass
class TagInfo:
    tag: str
    asset: str = "unknown"
    cls: RegClass = RegClass.unknown
    kind: str = "holding"            # holding | input | coil | discrete (Modbus) ; node (OPC UA)
    address: int | None = None
    scale: float = 1.0
    limits: tuple[float, float] | None = None
    envelope: tuple[float, float] | None = None
    writable_while_running: bool = True
    safe_direction: SafeDirection = SafeDirection.none
    is_trap: bool = False
    ttl_s: float = 2.0               # read freshness window
    node_id: str | None = None    # OPC UA


@dataclass
class AssetInfo:
    asset: str
    kind: str = "unknown"
    tags: list[str] = field(default_factory=list)


@dataclass
class World:
    plant_id: str = "plant"
    modbus_port: int | None = None
    opcua_port: int | None = None
    tags: dict[str, TagInfo] = field(default_factory=dict)
    assets: dict[str, AssetInfo] = field(default_factory=dict)
    _by_addr: dict[tuple[str, int], str] = field(default_factory=dict, repr=False)
    _by_node: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.reindex()

    def reindex(self):
        self._by_addr = {}
        self._by_node = {}
        for t in self.tags.values():
            if t.address is not None:
                self._by_addr[(t.kind, int(t.address))] = t.tag
            if t.node_id:
                self._by_node[t.node_id] = t.tag

    def add(self, t: TagInfo):
        self.tags[t.tag] = t
        self.assets.setdefault(t.asset, AssetInfo(asset=t.asset)).tags.append(t.tag)
        self.reindex()

    def tag_at(self, kind: str, address: int) -> TagInfo | None:
        tag = self._by_addr.get((kind, int(address)))
        return self.tags.get(tag) if tag else None

    def tag_for_node(self, node_id: str) -> TagInfo | None:
        tag = self._by_node.get(node_id)
        return self.tags.get(tag) if tag else None

    def traps(self) -> set[str]:
        return {t.tag for t in self.tags.values() if t.is_trap or t.cls == RegClass.trap}

    @classmethod
    def from_dict(cls, d: dict) -> World:
        w = cls(plant_id=str(d.get("plant_id", "plant")), modbus_port=d.get("modbus_port"), opcua_port=d.get("opcua_port"))
        tags = d.get("tags")
        if tags is None:   # flat form: every dict value with a "modbus" or "cls" key is a tag
            tags = {k: v for k, v in d.items() if isinstance(v, dict) and ("modbus" in v or "cls" in v)}
        for tag, v in tags.items():
            mb = v.get("modbus") or {}
            lim = v.get("limits"); env = v.get("envelope")
            cls_raw = v.get("cls", "unknown")
            try:
                rc = RegClass(cls_raw)
            except ValueError:
                rc = RegClass.unknown
            t = TagInfo(
                tag=tag, asset=str(v.get("asset", "unknown")), cls=rc,
                kind=str(mb.get("kind", v.get("kind", "holding"))),
                address=mb.get("address", v.get("address")), scale=float(mb.get("scale", v.get("scale", 1.0)) or 1.0),
                limits=tuple(lim) if lim else None, envelope=tuple(env) if env else None,
                writable_while_running=bool(v.get("writable_while_running", True)),
                safe_direction=SafeDirection(v.get("safe_direction", "none")),
                is_trap=bool(v.get("is_trap", False)) or rc == RegClass.trap,
                ttl_s=float(v.get("ttl_s", 2.0)),
                node_id=(v.get("opcua") or {}).get("node_id", v.get("node_id")),
            )
            w.tags[tag] = t
            w.assets.setdefault(t.asset, AssetInfo(asset=t.asset, kind=str((d.get("assets") or {}).get(t.asset, {}).get("kind", "unknown")))).tags.append(tag)
        w.reindex()
        return w

    @classmethod
    def load(cls, path: str | Path) -> World:
        return cls.from_dict(json.loads(Path(path).read_text()))


@dataclass
class TagValue:
    value: float
    age_s: float = 0.0


@dataclass
class PlantState:
    """What the gateway last observed. All optional; unknown is a legal value."""
    tags: dict[str, TagValue] = field(default_factory=dict)
    assets: dict[str, AssetState] = field(default_factory=dict)
    mode: PlantMode = PlantMode.production
    permit_open: bool = False
    alarm_active: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> PlantState:
        d = d or {}
        tags = {}
        for k, v in (d.get("tags") or {}).items():
            if isinstance(v, dict):
                tags[k] = TagValue(float(v.get("value", 0.0)), float(v.get("age_s", 0.0)))
            else:
                tags[k] = TagValue(float(v), 0.0)
        assets = {}
        for k, v in (d.get("assets") or {}).items():
            try:
                assets[k] = AssetState(v)
            except ValueError:
                assets[k] = AssetState.unknown
        try:
            mode = PlantMode(d.get("mode", "production"))
        except ValueError:
            mode = PlantMode.production
        return cls(tags=tags, assets=assets, mode=mode, permit_open=bool(d.get("permit_open", False)),
                   alarm_active=bool(d.get("alarm_active", False)))
