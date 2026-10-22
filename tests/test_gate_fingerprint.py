# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Map-free mode in the live gate: fingerprint parity with the builder, blind gate behaviour, render-mode selection."""
import asyncio
import random
from pathlib import Path

import pytest
from pymodbus.client import AsyncModbusTcpClient

from talos.fingerprints import FingerprintStore
from talos.gate import Gate, GateConfig
from talos.model import NullJudge
from talos.schema import Action, Op, Protocol, Request, Target, Tier
from talos.world import PlantState, World

FIX = Path(__file__).parent / "fixtures" / "world.json"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


class SpyJudge:
    """Records the token list the gate hands it; answers allow."""
    name = "spy"
    wants = "render_v01"
    supports_fp = True

    def __init__(self):
        self.bodies = []

    def judge(self, body, deadline_s=None):
        self.bodies.append(body)
        out = {"action": {"allow": 1.0, "cache": 0.0, "defer": 0.0, "deny": 0.0}, "envelope": {"inside_typical": 1.0},
               "severity": {"score": 0.0, "probabilities": {"0": 1.0}}}
        for q in ("needed_now", "duplicate", "servable", "over_budget", "contradiction", "state_conflict", "misbehaving"):
            out[q] = 0.0
        return out


def req(agent, op, tag, value=None):
    return Request(agent_id=agent, protocol=Protocol.modbus, op=op, target=Target(tag=tag), value=value)


# ---------------------------------------------------------------------------------------------------------------------
def test_fingerprint_parity_with_builder():
    """The same traffic through the builder's TagState and the gate's store yields identical fingerprints, request by request."""
    TagState = pytest.importorskip("data.build_records", reason="private record builder").TagState
    rng = random.Random(3)
    agents = [("hmi", "hmi"), ("a:cop", "copilot"), ("a:opt", "optimizer"), ("a:bad", "unknown")]
    builder, store = TagState(), FingerprintStore()
    ts = 0.0
    value = 50.0
    for i in range(400):
        ts += rng.uniform(0.1, 20.0)
        aid, role = rng.choice(agents)
        op = "write" if rng.random() < 0.3 else "read"
        if op == "write":
            v = round(value + rng.uniform(-15, 15), 2)
        else:
            value += rng.choice([0.0, 0.0, 0.3, -0.3])     # the plant moves the value on its own sometimes
            v = round(value, 2)
        ok = rng.random() > 0.05
        fb = builder.fingerprint(aid, ts, op, v if op == "write" else None)
        fg = store.fingerprint("t:x", aid, ts, op, v if op == "write" else None)
        assert fb.model_dump() == fg.model_dump(), f"row {i}: {fb} != {fg}"
        # builder: one observe call; gate: touch at decide time, value observation from the data path
        builder.observe(aid, role, ts, op, v, ok)
        store.touch("t:x", aid)
        if ok:
            if op == "write":
                store.observe_write("t:x", v, ts, agent_id=aid, role=role)
                value = v
            else:
                store.observe_read("t:x", v)
    assert store.get("t:x").fingerprint("a:cop", ts + 1, "write", value).writers == len(builder.writers) > 1


# ---------------------------------------------------------------------------------------------------------------------
def test_blind_gate_coldstart_defers_then_strikes():
    c = Clock()
    g = Gate(GateConfig(), world=None, judge=NullJudge(), clock=c)
    assert g.blind
    g.registry.register("a:new", role="copilot", tier=Tier.normal, now=c())
    d1 = g.decide(req("a:new", Op.write, "addr:4007", 12.0), PlantState())
    assert d1.action == Action.defer and any(r.endswith("coldstart:unfamiliar_write") for r in d1.reasons)
    assert not any("coldstart:repeat" in r for r in d1.reasons)
    assert g.registry.agents["a:new"].tier == Tier.normal
    c.tick(6)
    d2 = g.decide(req("a:new", Op.write, "addr:4007", 12.0), PlantState())
    assert d2.action == Action.defer and any("coldstart:repeat" in r for r in d2.reasons)
    assert True in g.registry.agents["a:new"].bad_hits
    # a register with write history (the HMI wrote it) is not cold: the write goes on to the model (NullJudge -> allow)
    g.registry.register("hmi", role="hmi", tier=Tier.trusted, now=c())
    g.observe_write("addr:4010", 40.0, c(), agent_id="hmi")
    c.tick(1)
    d3 = g.decide(req("a:new", Op.write, "addr:4010", 41.0), PlantState())
    assert d3.action != Action.deny and not any("coldstart" in r for r in d3.reasons)


def test_blind_gate_flood_still_quarantines():
    c = Clock()
    g = Gate(GateConfig(), world=None, judge=NullJudge(), clock=c)
    g.registry.register("a:flood", role="copilot", tier=Tier.normal, now=c())
    tiers = []
    for i in range(400):
        d = g.decide(req("a:flood", Op.read, "addr:100"), PlantState())
        tiers.append(d.tier); c.tick(0.01)
    assert Tier.quarantine in tiers


def test_blind_gate_unseen_reads_pass_but_are_flagged_and_rendered_blind():
    c = Clock()
    j = SpyJudge()
    g = Gate(GateConfig(), world=None, judge=j, clock=c)
    g.registry.register("a:scan", role="copilot", tier=Tier.normal, now=c())
    for addr in range(500, 520):
        d = g.decide(req("a:scan", Op.read, f"addr:{addr}"), PlantState())
        assert d.action == Action.allow and any(r.endswith("blind") for r in d.reasons)
        c.tick(0.5)
    toks = j.bodies[0]["tokens"]
    assert "FP_SEEN_ANY=no" in toks and not any(t.startswith("CLS=") for t in toks)
    assert j.bodies[0]["render_mode"] == "fingerprint"
    # by the 20th address the agent has touched 20 never-seen registers: visible to the model as a scanning pattern
    assert any(t.startswith("TAGS1M=c") and t != "TAGS1M=c0" for t in j.bodies[-1]["tokens"])


# ---------------------------------------------------------------------------------------------------------------------
def test_auto_mode_picks_rendering_per_tag():
    c = Clock()
    j = SpyJudge()
    g = Gate(GateConfig(), world=World.load(FIX), judge=j, clock=c)
    assert not g.blind
    g.registry.register("a:good", role="copilot", tier=Tier.normal, now=c())
    plant = PlantState.from_dict({"tags": {"t:pump1.speed_ref": {"value": 4000.0, "age_s": 0.2}}, "assets": {"pump1": "running"}})
    g.decide(req("a:good", Op.write, "t:pump1.speed_ref", 4100.0), plant)
    known = j.bodies[-1]
    assert known["render_mode"] == "declared_fp"
    assert any(t.startswith("CLS=") for t in known["tokens"]) and any(t.startswith("FP_") for t in known["tokens"])
    # an unmapped address on a mapped plant: still denied for writes (unknown_target), rendered blind for reads
    c.tick(1)
    d = g.decide(req("a:good", Op.write, "addr:9999", 1.0), plant)
    assert d.action == Action.deny and any(r.endswith("unknown_target") for r in d.reasons)
    c.tick(1)
    g.decide(req("a:good", Op.read, "addr:9998"), plant)
    assert j.bodies[-1]["render_mode"] == "fingerprint" and not any(t.startswith("CLS=") for t in j.bodies[-1]["tokens"])
    # explicit modes override
    g2 = Gate(GateConfig(map_mode="declared"), world=World.load(FIX), judge=SpyJudge(), clock=c)
    g2.registry.register("a:good", role="copilot", tier=Tier.normal, now=c())
    g2.decide(req("a:good", Op.write, "t:pump1.speed_ref", 4100.0), plant)
    assert g2.judge.bodies[-1]["render_mode"] == "declared" and not any(t.startswith("FP_") for t in g2.judge.bodies[-1]["tokens"])


def test_judge_without_fp_vocab_gets_declared():
    class OldJudge(SpyJudge):
        supports_fp = False
    c = Clock()
    g = Gate(GateConfig(), world=World.load(FIX), judge=OldJudge(), clock=c)
    g.registry.register("a:good", role="copilot", tier=Tier.normal, now=c())
    plant = PlantState.from_dict({"tags": {"t:pump1.speed_ref": {"value": 4000.0, "age_s": 0.2}}, "assets": {"pump1": "running"}})
    g.decide(req("a:good", Op.write, "t:pump1.speed_ref", 4100.0), plant)
    assert g.judge.bodies[-1]["render_mode"] == "declared"


# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_proxy_blind_mode_roundtrip(tmp_path):
    from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
    from pymodbus.server import ModbusTcpServer

    from talos.gateway.modbus_proxy import ModbusProxy
    from talos.ledger import Ledger
    from tests.test_gateway_proxy import FixedJudge, free_port, stop_stack
    hv = [0] * 400; hv[100] = 4000
    ctx = ModbusServerContext(devices=ModbusDeviceContext(hr=ModbusSequentialDataBlock(1, hv), ir=ModbusSequentialDataBlock(1, [0] * 400),
                                                          co=ModbusSequentialDataBlock(1, [0] * 64), di=ModbusSequentialDataBlock(1, [0] * 64)), single=True)
    up_port = free_port()
    server = ModbusTcpServer(ctx, address=("127.0.0.1", up_port))
    srv_task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    world = World()                       # no map at all
    gate = Gate(GateConfig(model_deadline_ms=50), world=world, judge=FixedJudge(), ledger=Ledger(tmp_path / "ledger.jsonl"))
    proxy = ModbusProxy(gate, ("127.0.0.1", up_port), world, plant_state_fn=lambda: PlantState())
    px_port = free_port()
    psrv = await asyncio.start_server(proxy.handle, "127.0.0.1", px_port)
    try:
        gate.registry.register("a:test", role="copilot")
        gate.registry.register_source("modbus", {"ip": "127.0.0.1", "unit": 1}, "a:test")
        c = AsyncModbusTcpClient("127.0.0.1", port=px_port)
        await c.connect()
        r = await c.read_holding_registers(100, count=1)
        assert not r.isError() and r.registers == [4000]
        assert gate.fingerprints.get("addr:100").last_obs == 4000.0          # observed raw on the wire
        r2 = await c.read_holding_registers(100, count=1)                     # served from the raw value cache
        assert r2.registers == [4000] and proxy.counts["served_cache"] == 1
        w = await c.write_register(100, 4100)                                 # never written by anyone: held (busy), not refused
        assert w.isError() and proxy.counts["deferred"] == 1
        rows = [l for l in (tmp_path / "ledger.jsonl").read_text().splitlines() if "coldstart:unfamiliar_write" in l]
        assert rows
        c.close()
    finally:
        await stop_stack(server, srv_task, psrv)
