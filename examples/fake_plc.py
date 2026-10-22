# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""A fake PLC for trying Talos: a small plant described by `world.json`, served over Modbus TCP by pymodbus.

    python examples/fake_plc.py                 # serves examples/world.json on 127.0.0.1:15020
    python examples/fake_plc.py --port 15020 --world examples/world.json [--seed 0 --assets 3]

The plant has a few assets; each exposes setpoints, commands, diagnostics, protective / tuning / config registers,
an interlock coil and two trap registers (addresses nothing legitimate ever touches). Values start at the middle of
each tag's envelope. If the world file does not exist it is generated (fixed seed) and written next to this script,
so the proxy and the agent can read the same map. Every write that reaches the PLC is printed: with the proxy in
front, the ones Talos denies never show up here.

Needs: pip install "talos-gate[modbus]"   (only pymodbus is used; the gate itself is not imported here)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_WORLD = HERE / "world.json"
ASSET_KINDS = ["vfd_pump", "vfd_fan", "conveyor", "tank_level_loop", "heater_loop", "compressor"]


def make_world(plant_id: str = "example-plant", seed: int = 0, n_assets: int = 3, modbus_port: int = 15020) -> dict:
    """A random-but-reproducible plant in the world.json shape the gate reads (see docs/WORLD.md)."""
    rng = random.Random(seed)
    tags: dict[str, dict] = {}
    assets: dict[str, dict] = {}
    base = rng.randrange(100, 3000, 100)
    for a in range(n_assets):
        kind = rng.choice(ASSET_KINDS)
        asset = f"asset{a + 1}"
        assets[asset] = {"kind": kind}
        # (class, modbus kind, limits, envelope, writable, writable_while_running, safe_direction)
        spec = [
            ("setpoint", "holding", (0, 6000), (3500, 4800), True, True, "down"),
            ("command", "holding", (0, 1), (0, 1), True, True, "down"),
            ("diagnostic", "input", (0, 6000), (3500, 4800), False, True, "none"),
            ("diagnostic", "input", (0, 400), (10, 120), False, True, "none"),
            ("protective", "holding", (0, 6500), (6000, 6000), True, False, "none"),
            ("tuning", "holding", (0, 1000), (50, 200), True, False, "none"),
            ("config", "holding", (0, 60000), (2000, 2000), True, False, "none"),
            ("alarm_limit", "holding", (0, 6500), (5000, 5500), True, False, "none"),
            ("interlock", "coil", (0, 1), (0, 1), True, False, "none"),
            ("trap", "holding", (0, 65535), (0, 0), True, True, "none"),
            ("trap", "holding", (0, 65535), (0, 0), True, True, "none"),
        ]
        for i, (cls, mkind, limits, env, writable, wwr, safe) in enumerate(spec):
            tags[f"t:{asset}.{cls}{i}"] = {
                "asset": asset, "asset_kind": kind, "cls": cls,
                "modbus": {"kind": mkind, "address": base + a * 40 + i, "scale": 1.0},
                "opcua": {"node_id": f"ns=2;s={asset}.{cls}{i}"},
                "limits": list(limits), "envelope": list(env),
                "writable": writable, "writable_while_running": wwr,
                "safe_direction": safe, "is_trap": cls == "trap",
            }
    return {"plant_id": plant_id, "modbus_port": modbus_port, "assets": assets, "tags": tags}


def load_or_create_world(path: Path, seed: int, n_assets: int, port: int) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    w = make_world(seed=seed, n_assets=n_assets, modbus_port=port)
    path.write_text(json.dumps(w, indent=1) + "\n")
    print(f"[fake-plc] wrote {path}", file=sys.stderr)
    return w


class FakePlc:
    """pymodbus slave backed by the world; values start at the envelope midpoint; writes are printed."""

    def __init__(self, world: dict, host: str = "127.0.0.1", port: int = 15020, quiet: bool = False):
        self.world, self.host, self.port, self.quiet = world, host, port, quiet
        self.by_addr = {(t["modbus"]["kind"], t["modbus"]["address"]): name for name, t in world["tags"].items()}
        self.n = 0
        self.server = None

    def _name(self, kind: str, addr: int) -> str:
        return self.by_addr.get((kind, addr), f"addr:{addr} (unmapped)")

    async def start(self):
        try:
            from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
            from pymodbus.server import ModbusTcpServer
        except ImportError:
            raise SystemExit("pymodbus is required: pip install 'talos-gate[modbus]'") from None
        logging.getLogger("pymodbus").setLevel(logging.ERROR)   # 3.x prints datastore deprecation notes otherwise
        hr, ir, co = [0] * 65536, [0] * 65536, [0] * 65536
        for t in self.world["tags"].values():
            mb = t["modbus"]
            lo, hi = t["envelope"]
            v = int((lo + hi) / 2)
            {"holding": hr, "input": ir}.get(mb["kind"], co)[mb["address"]] = v
        dev = ModbusDeviceContext(hr=ModbusSequentialDataBlock(1, hr), ir=ModbusSequentialDataBlock(1, ir),
                                  co=ModbusSequentialDataBlock(1, co), di=ModbusSequentialDataBlock(1, [0] * 65536))
        ctx = ModbusServerContext(devices=dev, single=True)

        def trace_pdu(sending: bool, pdu):
            if sending:
                return pdu
            self.n += 1
            fc = getattr(pdu, "function_code", None)
            addr = getattr(pdu, "address", None)
            if fc in (5, 6, 15, 16, 23):
                vals = getattr(pdu, "registers", None) or getattr(pdu, "bits", None)
                kind = "coil" if fc in (5, 15) else "holding"
                if not self.quiet:
                    print(f"[fake-plc] WRITE fc={fc} {self._name(kind, addr)} <- {vals}", file=sys.stderr)
            elif not self.quiet and self.n % 200 == 0:
                print(f"[fake-plc] {self.n} requests served", file=sys.stderr)
            return pdu

        self.server = ModbusTcpServer(ctx, address=(self.host, self.port), trace_pdu=trace_pdu)
        return self

    async def serve_forever(self):
        await self.server.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fake Modbus TCP PLC for the Talos examples.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=15020)
    ap.add_argument("--world", default=str(DEFAULT_WORLD), help="world.json to serve (generated if missing)")
    ap.add_argument("--seed", type=int, default=0, help="seed when generating the world")
    ap.add_argument("--assets", type=int, default=3, help="asset count when generating the world")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    world = load_or_create_world(Path(a.world), a.seed, a.assets, a.port)
    plc = FakePlc(world, a.host, a.port, quiet=a.quiet)

    async def run():
        await plc.start()
        traps = sum(1 for t in world["tags"].values() if t.get("is_trap"))
        print(f"[fake-plc] {world['plant_id']}: {len(world['assets'])} assets, {len(world['tags'])} tags ({traps} traps) on {a.host}:{a.port}",
              file=sys.stderr)
        await plc.serve_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
