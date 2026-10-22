# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
import asyncio
import contextlib
import json
import socket
import struct
from pathlib import Path

import pytest
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import ModbusTcpServer

from talos.gate import Gate, GateConfig
from talos.gateway.modbus_proxy import ModbusProxy, build_read_response, parse_pdu, parse_read_response
from talos.ledger import Ledger
from talos.schema import Tier
from talos.world import PlantState, World

FIX = Path(__file__).parent / "fixtures" / "world.json"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class FixedJudge:
    name = "fixed"

    def __init__(self, deny=0.0):
        self.deny = deny; self.calls = 0

    def judge(self, body, deadline_s=None):
        self.calls += 1
        out = {"action": {"allow": 1 - self.deny, "cache": 0, "defer": 0, "deny": self.deny}, "envelope": {"inside_typical": 1.0},
               "severity": {"score": 0.0, "probabilities": {"0": 1.0}}}
        for q in ("needed_now", "duplicate", "servable", "over_budget", "contradiction", "state_conflict", "misbehaving"):
            out[q] = 0.0
        return out


async def start_stack(tmp_path, judge=None, stop_bypass=True):
    hv = [0] * 400; hv[100] = 4000; hv[101] = 1; hv[110] = 6000; hv[300] = 60
    iv = [0] * 400; iv[200] = 4000
    hr = ModbusSequentialDataBlock(1, hv)
    ir = ModbusSequentialDataBlock(1, iv)
    ctx = ModbusServerContext(devices=ModbusDeviceContext(hr=hr, ir=ir, co=ModbusSequentialDataBlock(1, [0] * 64), di=ModbusSequentialDataBlock(1, [0] * 64)), single=True)
    up_port = free_port()
    server = ModbusTcpServer(ctx, address=("127.0.0.1", up_port))
    srv_task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    world = World.load(FIX)
    gate = Gate(GateConfig(stop_bypass=stop_bypass, model_deadline_ms=50), world=world, judge=judge or FixedJudge(), ledger=Ledger(tmp_path / "ledger.jsonl"))
    gate.plant = {"assets": {"pump1": "running", "fan1": "running"}}
    proxy = ModbusProxy(gate, ("127.0.0.1", up_port), world, plant_state_fn=lambda: PlantState.from_dict(gate.plant))
    px_port = free_port()
    psrv = await asyncio.start_server(proxy.handle, "127.0.0.1", px_port)
    up = AsyncModbusTcpClient("127.0.0.1", port=up_port)
    await up.connect()
    return server, srv_task, psrv, proxy, gate, px_port, up


async def stop_stack(server, srv_task, psrv, up=None):
    if up is not None:
        up.close()
    psrv.close()                      # no wait_closed(): 3.12 would block on handlers of still-open client sockets
    await asyncio.sleep(0.05)
    await server.shutdown(); srv_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await srv_task


def test_pdu_parsing_roundtrip():
    p = parse_pdu(bytes([16]) + struct.pack(">HHB", 100, 2, 4) + struct.pack(">HH", 1, 2))
    assert p["fc"] == 16 and p["writes"] == [("holding", 100, 1.0), ("holding", 101, 2.0)]
    p = parse_pdu(bytes([3]) + struct.pack(">HH", 200, 3))
    assert p["reads"] == [("holding", 200, 3)]
    assert parse_pdu(bytes([0x2B, 0, 0]))["unknown"] is True
    r = build_read_response(3, [1.0, 65535.0])
    assert parse_read_response(3, r, 2) == [1.0, 65535.0]
    r = build_read_response(1, [1, 0, 1])
    assert parse_read_response(1, r, 3) == [1.0, 0.0, 1.0]


@pytest.mark.asyncio
async def test_proxy_roundtrip_and_policies(tmp_path):
    judge = FixedJudge()
    server, srv_task, psrv, proxy, gate, port, up = await start_stack(tmp_path, judge=judge)
    try:
        gate.registry.register("a:test", role="copilot")
        gate.registry.register_source("modbus", {"ip": "127.0.0.1", "unit": 1}, "a:test")
        c = AsyncModbusTcpClient("127.0.0.1", port=port)
        await c.connect()
        # read forwarded, value cached
        r = await c.read_holding_registers(100, count=1)
        assert not r.isError() and r.registers == [4000]
        assert proxy.counts["forwarded"] == 1
        r2 = await c.read_holding_registers(100, count=1)
        assert r2.registers == [4000] and proxy.counts["served_cache"] == 1
        # write inside limits allowed and forwarded; upstream changed
        w = await c.write_register(100, 4200)
        assert not w.isError() and (await up.read_holding_registers(100, count=1)).registers == [4200]
        # duplicate write -> served from cache (ack, not forwarded)
        fwd = proxy.counts["forwarded"]
        w2 = await c.write_register(100, 4200)
        assert not w2.isError() and proxy.counts["forwarded"] == fwd
        # out of limits -> exception 0x04, upstream unchanged
        w3 = await c.write_register(100, 9000)
        assert w3.isError() and w3.exception_code == 4 and (await up.read_holding_registers(100, count=1)).registers == [4200]
        # protective register from a normal-tier unregistered source -> denied
        w4 = await c.write_register(110, 5500)
        assert w4.isError() and (await up.read_holding_registers(110, count=1)).registers == [6000]
        # stop (speed -> 0) always passes
        w5 = await c.write_register(100, 0)
        assert not w5.isError() and (await up.read_holding_registers(100, count=1)).registers == [0]
        # trap register: first touch denied + quarantine; second touch -> blocked and connection closed
        t1 = await c.write_register(150, 1)
        assert t1.isError()
        aid = "a:test"
        assert gate.registry.state(aid).tier == Tier.quarantine
        t2 = await c.read_holding_registers(150, count=1)
        assert t2.isError() or not c.connected
        await asyncio.sleep(0.1)
        assert gate.registry.state(aid).tier == Tier.blocked
        c.close()
        # unknown function code -> exception 0x01
        rd, wr = await asyncio.open_connection("127.0.0.1", port)
        pdu = bytes([0x2B, 0x0E, 0x01, 0x00])
        wr.write(struct.pack(">HHHB", 7, 0, len(pdu) + 1, 1) + pdu); await wr.drain()
        resp = await asyncio.wait_for(rd.read(64), 2)
        assert resp[7] == 0x2B | 0x80 and resp[8] == 0x01
        wr.close(); await wr.wait_closed()
        assert proxy.counts["unknown_fc"] == 1
        assert gate.ledger.verify()[0] is True or True   # chain checked after close below
        assert max(proxy.latencies) < 50
    finally:
        await stop_stack(server, srv_task, psrv, up)
        gate.ledger.close()
    assert Ledger(tmp_path / "ledger.jsonl").verify() == (True, None)


@pytest.mark.asyncio
async def test_proxy_busy_on_budget(tmp_path):
    server, srv_task, psrv, proxy, gate, port, up = await start_stack(tmp_path)
    try:
        c = AsyncModbusTcpClient("127.0.0.1", port=port)
        await c.connect()
        codes = []
        for i in range(12):
            w = await c.write_register(100, 4000 + i)
            codes.append(w.exception_code if w.isError() else 0)
        assert 6 in codes            # unregistered source has a tiny write budget -> busy
        c.close()
    finally:
        await stop_stack(server, srv_task, psrv, up)
        gate.ledger.close()


async def test_proxy_keeps_connection_when_judging_raises(tmp_path):
    """Bench regression: an exception while judging one frame used to drop the client mid-sequence (a stop never landed).
    Now that frame gets a Modbus exception and the next frame is served."""
    server, srv_task, psrv, proxy, gate, port, up = await start_stack(tmp_path)
    try:
        real = proxy._process
        state = {"boom": True}

        async def flaky(*a, **k):
            if state["boom"]:
                state["boom"] = False
                raise RuntimeError("simulated gate bug")
            return await real(*a, **k)
        proxy._process = flaky
        c = AsyncModbusTcpClient("127.0.0.1", port=port)
        await c.connect()
        r1 = await c.read_holding_registers(100, count=1)
        assert r1.isError()                                   # that frame answered with an exception...
        r2 = await c.read_holding_registers(100, count=1)
        assert not r2.isError() and r2.registers == [4000]    # ...and the connection survived
        assert proxy.counts["errors"] == 1
        c.close()
    finally:
        await stop_stack(server, srv_task, psrv, up)
        gate.ledger.close()


async def test_proxy_slow_judge_degrades_instead_of_dropping(tmp_path):
    import time as _t

    class Slow(FixedJudge):
        def judge(self, body, deadline_s=None):
            _t.sleep(0.15); return super().judge(body, deadline_s)
    server, srv_task, psrv, proxy, gate, port, up = await start_stack(tmp_path, judge=Slow())   # deadline 50 ms in start_stack
    try:
        c = AsyncModbusTcpClient("127.0.0.1", port=port)
        await c.connect()
        w = await c.write_register(100, 4100)
        assert w.isError() and w.exception_code == 6          # busy (defer), not a dropped socket
        r = await c.read_holding_registers(100, count=1)
        assert r.registers == [4000] or r.isError()           # still talking on the same connection
        c.close(); await asyncio.sleep(0.3)
    finally:
        await stop_stack(server, srv_task, psrv, up)
        gate.ledger.close()


async def test_bookkeeping_error_after_forward_does_not_fail_a_landed_write(tmp_path):
    server, srv_task, psrv, proxy, gate, port, up = await start_stack(tmp_path)
    try:
        def boom(*a, **k):
            raise RuntimeError("bookkeeping bug")
        gate.observe_write = boom
        c = AsyncModbusTcpClient("127.0.0.1", port=port)
        await c.connect()
        w = await c.write_register(100, 4250)
        assert not w.isError()                                  # the controller executed it and the client is told so
        r = await up.read_holding_registers(100, count=1)
        assert r.registers == [4250]
        assert proxy.counts["errors"] == 1
        c.close()
    finally:
        await stop_stack(server, srv_task, psrv, up)
        gate.ledger.close()


async def test_dead_upstream_closes_the_client(tmp_path):
    """The controller side vanishing must reach the client as a disconnect, not as an endless stream of deny frames."""
    async def slam(reader, writer):
        writer.close()
    dead = await asyncio.start_server(slam, "127.0.0.1", 0)
    dport = dead.sockets[0].getsockname()[1]
    gate = Gate(GateConfig(), world=World.load(FIX), judge=FixedJudge(), ledger=Ledger(tmp_path / "l.jsonl"))
    proxy = ModbusProxy(gate, ("127.0.0.1", dport), gate.world, plant_state_fn=lambda: PlantState())
    psrv = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    pport = psrv.sockets[0].getsockname()[1]
    try:
        r, w = await asyncio.open_connection("127.0.0.1", pport)
        w.write(struct.pack(">HHHB", 1, 0, 6, 1) + bytes([3, 0, 100, 0, 1])); await w.drain()
        data = await asyncio.wait_for(r.read(64), timeout=2.0)
        assert data == b""                                       # closed, no exception frame
        w.close()
    finally:
        psrv.close(); dead.close()
        gate.ledger.close()


async def test_error_frames_are_ledgered(tmp_path):
    server, srv_task, psrv, proxy, gate, port, up = await start_stack(tmp_path)
    try:
        async def boom(*a, **k):
            raise RuntimeError("gate bug")
        proxy._process = boom
        c = AsyncModbusTcpClient("127.0.0.1", port=port)
        await c.connect()
        r = await c.read_holding_registers(100, count=1)
        assert r.isError() and proxy.counts["denied"] == 1
        c.close()
        gate.ledger.close()
        rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines() if line.strip()]
        assert rows and rows[-1]["by"] == "error" and rows[-1]["reasons"] == ["proxy_error:RuntimeError"]
    finally:
        await stop_stack(server, srv_task, psrv, up)
