# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
import asyncio
import contextlib
import json
import socket
import sys
from pathlib import Path

import pytest
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import ModbusTcpServer

from talos.gate import Gate, GateConfig
from talos.gateway.mcp_server import McpGateway, McpServer, ModbusClient, parse_agent, read_pdu, write_pdu
from talos.gateway.modbus_proxy import parse_pdu
from talos.ledger import Ledger
from talos.schema import Tier
from talos.world import PlantState, World

FIX = Path(__file__).parent / "fixtures" / "world.json"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class FixedJudge:
    name = "fixed"

    def judge(self, body, deadline_s=None):
        out = {"action": {"allow": 1.0, "cache": 0, "defer": 0, "deny": 0.0}, "envelope": {"inside_typical": 1.0},
               "severity": {"score": 0.0, "probabilities": {"0": 1.0}}}
        for q in ("needed_now", "duplicate", "servable", "over_budget", "contradiction", "state_conflict", "misbehaving"):
            out[q] = 0.0
        return out


@contextlib.asynccontextmanager
async def plc():
    hv = [0] * 400; hv[100] = 4000; hv[101] = 1; hv[110] = 6000; hv[300] = 60
    iv = [0] * 400; iv[200] = 4000
    ctx = ModbusServerContext(devices=ModbusDeviceContext(
        hr=ModbusSequentialDataBlock(1, hv), ir=ModbusSequentialDataBlock(1, iv),
        co=ModbusSequentialDataBlock(1, [0] * 64), di=ModbusSequentialDataBlock(1, [0] * 64)), single=True)
    port = free_port()
    server = ModbusTcpServer(ctx, address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    up = AsyncModbusTcpClient("127.0.0.1", port=port)
    await up.connect()
    try:
        yield port, up
    finally:
        up.close()
        await server.shutdown(); task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def make(tmp_path, port, agent=("a:copilot", "copilot"), explain=False, world=True, shadow=False):
    w = World.load(FIX) if world else World()
    gate = Gate(GateConfig(model_deadline_ms=50, shadow=shadow), world=w, judge=FixedJudge(), ledger=Ledger(tmp_path / "ledger.jsonl"))
    plant = {"assets": {"pump1": "running", "fan1": "running"}}
    if agent:
        gate.registry.register(agent[0], role=agent[1], tier=Tier.normal)
        aid = agent[0]
    else:
        aid = gate.registry.resolve("mcp", {"client_id": ""})
    gw = McpGateway(gate, w, ModbusClient("127.0.0.1", port), aid, explain=explain,
                    plant_state_fn=lambda: PlantState.from_dict(plant))
    return McpServer(gw), gate


async def call(server, name, args=None, mid=1):
    reply = await server.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                                 "params": {"name": name, "arguments": args or {}}})
    res = reply["result"]
    assert json.loads(res["content"][0]["text"]) == res["structuredContent"] if "structuredContent" in res else True
    return res


async def regs(up, addr):
    return (await up.read_holding_registers(addr, count=1)).registers[0]


def test_frames_match_what_the_proxy_parses():
    assert parse_pdu(read_pdu("holding", 100, 3))["reads"] == [("holding", 100, 3)]
    assert parse_pdu(read_pdu("discrete", 7, 9))["reads"] == [("discrete", 7, 9)]
    assert parse_pdu(write_pdu("holding", 100, [4200]))["writes"] == [("holding", 100, 4200.0)]
    assert parse_pdu(write_pdu("holding", 100, [1, 2]))["writes"] == [("holding", 100, 1.0), ("holding", 101, 2.0)]
    assert parse_pdu(write_pdu("coil", 5, [1]))["writes"] == [("coil", 5, 1.0)]
    vals = [1, 0, 1, 1, 0, 0, 0, 0, 1]
    assert parse_pdu(write_pdu("coil", 5, vals))["writes"] == [("coil", 5 + i, float(v)) for i, v in enumerate(vals)]


def test_parse_agent():
    assert parse_agent("copilot:copilot") == ("copilot", "copilot")
    assert parse_agent("x") == ("x", "unknown")
    assert parse_agent(None) is None
    with pytest.raises(SystemExit):
        parse_agent("x:wizard")


async def test_handshake_and_tool_list(tmp_path):
    server, gate = make(tmp_path, free_port())
    r = await server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    assert r["result"]["protocolVersion"] == "2025-06-18" and "tools" in r["result"]["capabilities"]
    r = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert r["result"]["protocolVersion"] == "2025-11-25"
    assert await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert (await server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"}))["result"] == {}
    names = [t["name"] for t in (await server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}))["result"]["tools"]]
    assert names == ["list_tags", "read_tag", "write_tag", "read_registers", "write_registers", "status"]
    assert (await server.handle({"jsonrpc": "2.0", "id": 4, "method": "server/discover"}))["error"]["code"] == -32601
    assert (await server.handle({"id": 5}))["error"]["code"] == -32600
    gate.ledger.close()


async def test_list_tags_never_shows_traps(tmp_path):
    server, gate = make(tmp_path, free_port())
    res = await call(server, "list_tags")
    tags = {t["tag"]: t for t in res["structuredContent"]["tags"]}
    assert "t:pump1.trap_a" not in tags and "t:pump1.speed_ref" in tags
    assert tags["t:pump1.speed_ref"]["writable"] is True and tags["t:pump1.speed_fb"]["writable"] is False
    assert tags["t:pump1.speed_ref"]["limits"] == [0, 6000]
    # a trap is not addressable by name either
    res = await call(server, "read_tag", {"tag": "t:pump1.trap_a"})
    assert res["isError"] and "unknown tag" in res["content"][0]["text"]
    gate.ledger.close()


async def test_reads_writes_and_policies_through_the_gate(tmp_path):
    async with plc() as (port, up):
        server, gate = make(tmp_path, port)
        gw = server.gw
        r = await call(server, "read_tag", {"tag": "t:pump1.speed_ref"})
        assert not r["isError"] and r["structuredContent"]["value"] == 4000
        assert r["structuredContent"]["action"] == "allow" and r["structuredContent"]["source"] == "controller"
        r = await call(server, "read_tag", {"tag": "t:pump1.speed_ref"})
        assert r["structuredContent"]["source"] == "cache" and gw.counts["served_cache"] == 1
        r = await call(server, "read_tag", {"tag": "t:pump1.speed_fb"})        # input register
        assert r["structuredContent"]["value"] == 4000
        # write inside limits: forwarded, controller changed
        r = await call(server, "write_tag", {"tag": "t:pump1.speed_ref", "value": 4200})
        assert not r["isError"] and r["structuredContent"]["written"] == 1 and await regs(up, 100) == 4200
        # duplicate write: acknowledged, controller not touched
        fwd = gw.counts["forwarded"]
        r = await call(server, "write_tag", {"tag": "t:pump1.speed_ref", "value": 4200})
        assert r["structuredContent"]["action"] == "cache" and gw.counts["forwarded"] == fwd
        # outside limits: denied, controller unchanged, reasons hidden by default
        r = await call(server, "write_tag", {"tag": "t:pump1.speed_ref", "value": 9000})
        assert r["isError"] and r["structuredContent"]["action"] == "deny" and await regs(up, 100) == 4200
        assert "reasons" not in r["structuredContent"]
        # protective register while the pump runs: denied
        r = await call(server, "write_tag", {"tag": "t:pump1.max_freq", "value": 5500})
        assert r["isError"] and await regs(up, 110) == 6000
        # a stop always passes
        r = await call(server, "write_tag", {"tag": "t:pump1.speed_ref", "value": 0})
        assert not r["isError"] and await regs(up, 100) == 0
        # every decision is in the hash-chained ledger under this agent
        gate.ledger.close()
        rows = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines() if x.strip()]
        assert rows and {row["agent"] for row in rows} == {"a:copilot"}
        assert Ledger(tmp_path / "ledger.jsonl").verify() == (True, None)


async def test_trap_quarantines_then_blocks(tmp_path):
    async with plc() as (port, up):
        server, gate = make(tmp_path, port)
        r = await call(server, "write_registers", {"kind": "holding", "address": 150, "values": [1]})
        assert r["isError"] and r["structuredContent"]["action"] == "deny"
        assert gate.registry.state("a:copilot").tier == Tier.quarantine
        r = await call(server, "read_registers", {"kind": "holding", "address": 150, "count": 1})
        assert r["isError"] and r["structuredContent"]["tier"] == "blocked"
        # blocked stays blocked, even for a legitimate read
        r = await call(server, "read_tag", {"tag": "t:pump1.speed_ref"})
        assert r["isError"] and r["structuredContent"]["tier"] == "blocked"
        st = (await call(server, "status"))["structuredContent"]
        assert st == {"agent": "a:copilot", "role": "copilot", "tier": "blocked", "registered": True}
        gate.ledger.close()


async def test_unregistered_agent_is_deferred_with_retry_hint(tmp_path):
    async with plc() as (port, up):
        server, gate = make(tmp_path, port, agent=None)
        assert server.gw.agent_id.startswith("u:")
        actions = []
        for i in range(12):
            r = await call(server, "write_tag", {"tag": "t:pump1.speed_ref", "value": 4000 + i})
            actions.append(r["structuredContent"])
        deferred = [a for a in actions if a["action"] == "defer"]
        assert deferred and all(a["retry_after_ms"] > 0 for a in deferred)
        gate.ledger.close()


async def test_raw_register_tools_and_explain(tmp_path):
    async with plc() as (port, up):
        server, gate = make(tmp_path, port, explain=True)
        r = await call(server, "read_registers", {"kind": "holding", "address": 100, "count": 2})
        assert r["structuredContent"]["values"] == [4000, 1]
        r = await call(server, "write_registers", {"kind": "holding", "address": 300, "values": [70]})
        assert not r["isError"] and await regs(up, 300) == 70
        r = await call(server, "write_registers", {"kind": "holding", "address": 300, "values": [500]})
        assert r["isError"] and r["structuredContent"]["reasons"]               # --explain shows why
        r = await call(server, "write_registers", {"kind": "coil", "address": 3, "values": [1, 0, 1]})
        assert r["isError"] and "unknown_target" in str(r["structuredContent"]["reasons"])  # unmapped write on a mapped plant
        assert (await up.read_coils(3, count=3)).bits[:3] == [False, False, False]
        gate.ledger.close()


async def test_blind_mode(tmp_path):
    async with plc() as (port, up):
        server, gate = make(tmp_path, port, world=False)
        assert (await call(server, "list_tags"))["structuredContent"]["tags"] == []
        r = await call(server, "read_registers", {"kind": "input", "address": 200})
        assert r["structuredContent"]["values"] == [4000]
        r = await call(server, "write_registers", {"kind": "coil", "address": 3, "values": [1, 0, 1]})
        assert r["isError"] and r["structuredContent"]["action"] == "defer"     # cold start: unknown register, wait
        assert (await up.read_coils(3, count=3)).bits[:3] == [False, False, False]
        gate.ledger.close()
        rows = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines() if x.strip()]
        assert rows[0]["tag"] == "addr:200"            # same unmapped key as the proxy and the training data


async def test_shadow_mode_forwards_and_ledgers_the_real_verdict(tmp_path):
    async with plc() as (port, up):
        server, gate = make(tmp_path, port, world=False, shadow=True)
        r = await call(server, "write_registers", {"kind": "coil", "address": 3, "values": [1, 0, 1]})
        assert not r["isError"] and r["structuredContent"]["written"] == 3
        assert (await up.read_coils(3, count=3)).bits[:3] == [True, False, True]
        gate.ledger.close()
        rows = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines() if x.strip()]
        assert len(rows) == 3 and rows[0]["shadow"] is True and rows[0]["action"] == "defer" and rows[0]["returned"] == "allow"


@pytest.mark.parametrize("name,args,msg", [
    ("read_tag", {}, "required"),
    ("read_tag", {"tag": "t:pump1.speed_ref", "extra": 1}, "takes"),
    ("read_tag", {"tag": 5}, "string"),
    ("write_tag", {"tag": "t:pump1.speed_fb", "value": 1}, "read-only"),
    ("write_tag", {"tag": "t:pump1.speed_ref", "value": "fast"}, "number"),
    ("write_tag", {"tag": "t:pump1.speed_ref", "value": 70000}, "16-bit"),
    ("read_registers", {"kind": "eeprom", "address": 1}, "kind"),
    ("read_registers", {"kind": ["holding"], "address": 1}, "kind"),
    ("read_registers", {"kind": "holding", "address": 65535, "count": 2}, "past"),
    ("read_registers", {"kind": "holding", "address": 1, "count": 126}, "count"),
    ("write_registers", {"kind": "input", "address": 1, "values": [1]}, "writable"),
    ("write_registers", {"kind": "coil", "address": 1, "values": [2]}, "integer"),
    ("write_registers", {"kind": "holding", "address": 1, "values": []}, "non-empty"),
    ("nope", {}, "unknown tool"),
])
async def test_malformed_calls_never_reach_the_gate(tmp_path, name, args, msg):
    server, gate = make(tmp_path, free_port())
    before = gate.stats["requests"]
    r = await call(server, name, args)
    assert r["isError"] and msg in r["content"][0]["text"]
    assert gate.stats["requests"] == before
    gate.ledger.close()


async def test_dead_controller_is_a_tool_error_not_a_crash(tmp_path):
    server, gate = make(tmp_path, free_port())          # nothing listening
    r = await call(server, "read_tag", {"tag": "t:pump1.speed_ref"})
    assert r["isError"] and "controller unreachable" in r["content"][0]["text"]
    assert server.gw.counts["upstream_errors"] == 1
    gate.ledger.close()


async def test_controller_exception_is_reported_and_counted(tmp_path):
    async with plc() as (port, up):
        server, gate = make(tmp_path, port)
        # past the end of the PLC's 400-register block: the controller answers with an exception
        server.gw.world.tags["t:pump1.speed_ref"].address = 9000
        server.gw.world.reindex()
        r = await call(server, "read_tag", {"tag": "t:pump1.speed_ref"})
        assert r["isError"] and r["structuredContent"]["controller_exception"] == 2
        assert len(gate.registry.state("a:copilot").errors) == 1
        gate.ledger.close()


async def test_bookkeeping_error_does_not_fail_a_landed_write(tmp_path):
    async with plc() as (port, up):
        server, gate = make(tmp_path, port)

        def boom(*a, **k):
            raise RuntimeError("bookkeeping bug")
        gate.observe_write = boom
        r = await call(server, "write_tag", {"tag": "t:pump1.speed_ref", "value": 4250})
        assert not r["isError"] and await regs(up, 100) == 4250 and server.gw.counts["errors"] == 1
        gate.ledger.close()


async def test_stdio_framing(tmp_path):
    server, gate = make(tmp_path, free_port())
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n'
                     b'[{"jsonrpc":"2.0","id":2,"method":"ping"},{"jsonrpc":"2.0","method":"notifications/x"}]\n')
    reader.feed_eof()
    out = []
    await server.serve(reader, out.append)
    assert out[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert out[1]["error"]["code"] == -32700
    assert out[2] == [{"jsonrpc": "2.0", "id": 2, "result": {}}]
    gate.ledger.close()


def is_error(res) -> bool:
    """`is_error` in mcp 2.x, `isError` in 1.x."""
    return bool(res.is_error if hasattr(res, "is_error") else res.isError)


async def test_official_mcp_client_over_stdio(tmp_path):
    """End to end with the reference MCP Python SDK as the client, the server as a real subprocess."""
    mcp = pytest.importorskip("mcp")
    from mcp.client.stdio import stdio_client
    async with plc() as (port, up):
        params = mcp.StdioServerParameters(command=sys.executable, args=[
            "-m", "talos.gateway.mcp_server", "--upstream", f"127.0.0.1:{port}", "--world", str(FIX), "--model", "null",
            "--agent", "copilot:copilot", "--ledger", str(tmp_path / "ledger.jsonl")])
        async with stdio_client(params) as (read, write), mcp.ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} >= {"list_tags", "read_tag", "write_tag"}
            res = await session.call_tool("read_tag", {"tag": "t:pump1.speed_ref"})
            assert not is_error(res) and json.loads(res.content[0].text)["value"] == 4000
            res = await session.call_tool("write_tag", {"tag": "t:pump1.speed_ref", "value": 4300})
            assert not is_error(res)
            assert await regs(up, 100) == 4300
            res = await session.call_tool("write_tag", {"tag": "t:pump1.speed_ref", "value": 9000})
            assert is_error(res) and await regs(up, 100) == 4300
