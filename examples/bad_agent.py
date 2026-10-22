# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Scripted agents that talk Modbus to the plant through the Talos proxy: one well-behaved, three that are not.

    python examples/bad_agent.py --target 127.0.0.1:15502 --script dashboard|flood|decoy_prober|drifter --seconds 20

  dashboard     polite: polls four diagnostics / setpoints every 3 s (the proxy lets it through)
  flood         polls everything as fast as the socket allows (over budget -> deferred, then quarantined)
  decoy_prober  walks the whole address space, trap registers and unmapped addresses included (traps -> deny)
  drifter       walks a setpoint out of its envelope in small steps (drift -> deny once the gate sees the pattern)

Each script talks as its own Modbus unit id (dashboard 1, decoy_prober 2, flood 3, drifter 4) so the proxy sees four
agents; register them with `talos-proxy --agent NAME=127.0.0.1:UNIT:ROLE` or watch them run on an unknown agent's budget.

What comes back from the proxy: a normal response (ok), Modbus exception 0x04 (Talos denied), 0x06 (Talos deferred:
over budget / quarantined, retry later), 0x01 (illegal function), or a closed connection (agent blocked).

Needs: pip install "talos-gate[modbus] @ git+https://github.com/tractian/talos"   (pymodbus client only; the gate is not imported here)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_WORLD = HERE / "world.json"
EXC_NAMES = {1: "illegal-function", 2: "illegal-address", 4: "DENIED", 6: "DEFERRED(busy)"}


UNIT_IDS = {"dashboard": 1, "decoy_prober": 2, "flood": 3, "drifter": 4}   # one Modbus unit id per script = one identity at the proxy


class PlantClient:
    """Thin pymodbus client over a world.json: read/write by tag name, outcome classified for the summary."""

    def __init__(self, host: str, port: int, world: dict, unit: int = 1):
        self.host, self.port, self.tags, self.unit = host, port, world["tags"], unit
        self.client = None
        self.counts = {"ok": 0, "denied": 0, "deferred": 0, "illegal": 0, "closed": 0, "error": 0}

    async def connect(self):
        try:
            from pymodbus.client import AsyncModbusTcpClient
        except ImportError:
            raise SystemExit("pymodbus is required: pip install 'talos-gate[modbus] @ git+https://github.com/tractian/talos'") from None
        self.client = AsyncModbusTcpClient(self.host, port=self.port)
        if not await self.client.connect():
            raise SystemExit(f"cannot connect to {self.host}:{self.port} (is the proxy running?)")

    def close(self):
        if self.client:
            self.client.close()

    async def _call(self, fn, *args, **kw) -> dict:
        t0 = time.perf_counter()
        try:
            r = await fn(*args, **kw)
        except Exception as e:   # noqa: BLE001  (pymodbus raises several connection error types)
            self.counts["closed"] += 1
            return {"ok": False, "status": "CLOSED", "detail": type(e).__name__, "ms": (time.perf_counter() - t0) * 1000}
        ms = (time.perf_counter() - t0) * 1000
        if r is None or getattr(r, "isError", lambda: True)():
            code = getattr(r, "exception_code", None)
            status = EXC_NAMES.get(code, f"exc-{code}")
            key = {4: "denied", 6: "deferred", 1: "illegal", 2: "illegal"}.get(code, "error")
            self.counts[key] += 1
            return {"ok": False, "status": status, "detail": code, "ms": ms}
        self.counts["ok"] += 1
        val = (getattr(r, "registers", None) or getattr(r, "bits", None) or [None])[0]
        return {"ok": True, "status": "ok", "value": val, "ms": ms}

    async def read(self, tag: str) -> dict:
        mb = self.tags[tag]["modbus"]
        return await self.read_addr(mb["kind"], mb["address"])

    async def read_addr(self, kind: str, addr: int) -> dict:
        c = self.client
        fn = {"holding": c.read_holding_registers, "input": c.read_input_registers, "coil": c.read_coils, "discrete": c.read_discrete_inputs}[kind]
        return await self._call(fn, addr, count=1, device_id=self.unit)

    async def write(self, tag: str, value: float) -> dict:
        mb = self.tags[tag]["modbus"]
        if mb["kind"] == "coil":
            return await self._call(self.client.write_coil, mb["address"], bool(value), device_id=self.unit)
        return await self._call(self.client.write_register, mb["address"], int(value) & 0xFFFF, device_id=self.unit)


def pick(tags: dict, cls=None, writable=None, include_traps=False) -> list[str]:
    out = []
    for k, v in tags.items():
        if not include_traps and v.get("is_trap"):
            continue
        if cls and v["cls"] not in (cls if isinstance(cls, (list, tuple)) else [cls]):
            continue
        if writable is not None and bool(v.get("writable", True)) != writable:
            continue
        out.append(k)
    return out


def band(tags: dict, tag: str) -> tuple[float, float]:
    t = tags[tag]
    b = t.get("envelope") or t.get("limits") or [0, 1]
    return float(b[0]), float(b[1])


def say(step: int, op: str, tag: str, r: dict, value=None):
    v = f" <- {value}" if op == "write" else (f" = {r.get('value')}" if r["ok"] else "")
    print(f"[{step:4d}] {op:5s} {tag:32s}{v:>10s}  {r['status']}  {r['ms']:.1f} ms", file=sys.stderr)


# ---- the four behaviours -----------------------------------------------------------------------------------------
async def dashboard(c: PlantClient, deadline: float, rng: random.Random, period_s: float = 3.0):
    sel = rng.sample(pick(c.tags, cls=["diagnostic", "setpoint"]), 4)
    step = 0
    while time.time() < deadline:
        for t in sel:
            r = await c.read(t); step += 1; say(step, "read", t, r)
        await asyncio.sleep(period_s)


async def flood(c: PlantClient, deadline: float, rng: random.Random):
    tags = pick(c.tags, cls="diagnostic")
    step = 0
    while time.time() < deadline:
        r = await c.read(tags[step % len(tags)]); step += 1
        if r["status"] == "CLOSED":
            say(step, "read", tags[(step - 1) % len(tags)], r); break
        if step % 100 == 0 or (not r["ok"] and c.counts["deferred"] in (1, 2, 3)):
            say(step, "read", tags[(step - 1) % len(tags)], r)


async def decoy_prober(c: PlantClient, deadline: float, rng: random.Random, probe_from: int = 5000, probe_n: int = 30, period_s: float = 0.1):
    tags = sorted(c.tags, key=lambda k: c.tags[k]["modbus"]["address"])
    step = 0
    for t in tags:
        if time.time() >= deadline:
            return
        r = await c.read(t); step += 1
        say(step, "read", t + (" [TRAP]" if c.tags[t].get("is_trap") else ""), r)
        if r["status"] == "CLOSED":
            return
        await asyncio.sleep(period_s)
    for addr in range(probe_from, probe_from + probe_n):
        if time.time() >= deadline:
            return
        r = await c.read_addr("holding", addr); step += 1
        say(step, "read", f"addr:{addr}", r)
        if r["status"] == "CLOSED":
            return
        await asyncio.sleep(period_s)


async def drifter(c: PlantClient, deadline: float, rng: random.Random, pct_per_step: float = 5.0, period_s: float = 2.0):
    t = pick(c.tags, cls="setpoint", writable=True)[0]
    lo, hi = band(c.tags, t)
    r = await c.read(t); say(0, "read", t, r)
    cur = float(r.get("value") or (lo + hi) / 2)
    step = 0
    while time.time() < deadline:
        cur += (hi - lo) * pct_per_step / 100
        r = await c.write(t, round(cur)); step += 1
        tagline = t + ("  (outside envelope)" if cur > hi else "")
        say(step, "write", tagline, r, round(cur))
        if r["status"] == "CLOSED":
            break
        await asyncio.sleep(period_s)


SCRIPTS = {"dashboard": dashboard, "flood": flood, "decoy_prober": decoy_prober, "drifter": drifter}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scripted Modbus agents for the Talos examples.")
    ap.add_argument("--target", default="127.0.0.1:15502", help="host:port of the Talos proxy (or the PLC, to compare)")
    ap.add_argument("--script", choices=sorted(SCRIPTS), required=True)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--world", default=str(DEFAULT_WORLD), help="the same world.json the fake PLC serves")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--unit", type=int, default=None, help="Modbus unit id (default: one per script, see UNIT_IDS)")
    a = ap.parse_args(argv)
    host, port = a.target.rsplit(":", 1)
    world = json.loads(Path(a.world).read_text())
    c = PlantClient(host, int(port), world, unit=a.unit if a.unit is not None else UNIT_IDS[a.script])

    async def run():
        await c.connect()
        print(f"[bad-agent] {a.script} -> {a.target} as unit {c.unit} for {a.seconds:.0f}s", file=sys.stderr)
        try:
            await asyncio.wait_for(SCRIPTS[a.script](c, time.time() + a.seconds, random.Random(a.seed)), timeout=a.seconds + 5)
        except TimeoutError:
            pass
        finally:
            c.close()
        print(f"[bad-agent] {a.script} done: " + " ".join(f"{k}={v}" for k, v in c.counts.items()), file=sys.stderr)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
