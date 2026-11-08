# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""MCP front end: an AI agent talks to the plant through MCP tools, and every tool call goes through the gate before it
reaches the controller.

    talos-mcp --upstream 127.0.0.1:15020 --world examples/world.json --weights weights --agent copilot:copilot

The agent's MCP client launches this as a stdio server (one process per agent, so the identity is whatever the operator
put on the command line, not anything the agent says about itself). Standard library only: newline-delimited JSON-RPC
2.0 on stdin/stdout, protocol revisions 2024-11-05 through 2025-11-25, no MCP SDK needed.

Tools:
  list_tags        the tags in the map an agent may use (trap registers are never listed)
  read_tag         read one mapped tag, in engineering units
  write_tag        write one mapped tag, in engineering units
  read_registers   raw Modbus read by kind/address/count (blind mode, or tags not in the map)
  write_registers  raw Modbus write (holding registers or coils), one frame, judged per register
  status           this agent's id, role and tier

Each call becomes one Modbus frame and is judged exactly like the proxy judges that frame: writes per register (one budget
token per frame), reads once per frame, the strictest decision wins. allow -> forwarded; cache -> answered from the gate's
value cache (reads) or acknowledged without touching the controller (duplicate writes); defer -> `retry_after_ms`;
deny -> refused; blocked tier -> refused for good. The agent gets the action, not the reasons, unless --explain is set:
telling a misbehaving client which rule it tripped helps it walk around the rule.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import struct
import sys
from typing import Any

import orjson

from talos.gate import Gate, GateConfig
from talos.gateway.modbus_proxy import make_judge, model_spec, parse_read_response
from talos.identity import ROLES
from talos.ledger import Ledger
from talos.schema import Action, Op, Protocol, RegClass, Request, Target, Tier
from talos.world import PlantState, TagInfo, World

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
READ_FC = {"holding": 3, "input": 4, "coil": 1, "discrete": 2}
MAX_READ = {"holding": 125, "input": 125, "coil": 2000, "discrete": 2000}
MAX_WRITE = {"holding": 123, "coil": 1968}
STRICTNESS = {Action.allow: 0, Action.cache: 1, Action.defer: 2, Action.deny: 3}


class ToolError(Exception):
    """A malformed tool call (unknown tag, bad address...). Reported to the agent as an MCP tool error; the controller
    is not touched and the gate is not consulted."""


class UpstreamError(Exception):
    pass


# ----------------------------------------------------------------------------------------------- Modbus TCP client
class ModbusClient:
    """One TCP connection to the controller, one request in flight at a time. Reconnects on the next call after a
    failure."""

    def __init__(self, host: str, port: int, timeout_s: float = 3.0):
        self.host, self.port, self.timeout_s = host, port, timeout_s
        self._rw: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._lock = asyncio.Lock()
        self._tid = 0

    async def request(self, uid: int, pdu: bytes) -> bytes:
        async with self._lock:
            try:
                return await asyncio.wait_for(self._roundtrip(uid, pdu), self.timeout_s)
            except (OSError, asyncio.IncompleteReadError, TimeoutError, asyncio.TimeoutError) as e:
                self.close()
                raise UpstreamError(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__) from e

    async def _roundtrip(self, uid: int, pdu: bytes) -> bytes:
        if self._rw is None:
            self._rw = await asyncio.open_connection(self.host, self.port)
        r, w = self._rw
        self._tid = (self._tid + 1) & 0xFFFF
        w.write(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, uid) + pdu)
        await w.drain()
        while True:
            tid, _, length, _ = struct.unpack(">HHHB", await r.readexactly(7))
            body = await r.readexactly(length - 1)
            if tid == self._tid:
                return body          # a stale answer to an earlier timed-out request is skipped

    def close(self):
        if self._rw is not None:
            self._rw[1].close()
            self._rw = None


def read_pdu(kind: str, address: int, count: int) -> bytes:
    return bytes([READ_FC[kind]]) + struct.pack(">HH", address, count)


def write_pdu(kind: str, address: int, values: list[int]) -> bytes:
    if kind == "coil":
        if len(values) == 1:
            return bytes([5]) + struct.pack(">HH", address, 0xFF00 if values[0] else 0)
        bits = bytearray((len(values) + 7) // 8)
        for i, v in enumerate(values):
            if v:
                bits[i // 8] |= 1 << (i % 8)
        return bytes([15]) + struct.pack(">HHB", address, len(values), len(bits)) + bytes(bits)
    if len(values) == 1:
        return bytes([6]) + struct.pack(">HH", address, values[0])
    return bytes([16]) + struct.pack(">HHB", address, len(values), 2 * len(values)) + struct.pack(f">{len(values)}H", *values)


# ---------------------------------------------------------------------------------------------------- the gateway
class McpGateway:
    """The protocol-independent part: typed calls in, gate decisions, Modbus frames out."""

    def __init__(self, gate: Gate, world: World, client: ModbusClient, agent_id: str, unit: int = 1,
                 explain: bool = False, plant_state_fn=None):
        self.gate, self.world, self.client = gate, world, client
        self.agent_id, self.unit, self.explain = agent_id, unit, explain
        self.plant_state_fn = plant_state_fn or (lambda: PlantState())
        self.counts = {"calls": 0, "forwarded": 0, "served_cache": 0, "denied": 0, "deferred": 0, "upstream_errors": 0,
                       "errors": 0}

    # -- map
    def _visible(self, t: TagInfo) -> bool:
        return t.address is not None and not t.is_trap and t.cls != RegClass.trap

    def list_tags(self) -> list[dict]:
        out = []
        for t in sorted(self.world.tags.values(), key=lambda t: t.tag):
            if not self._visible(t):
                continue
            row = {"tag": t.tag, "asset": t.asset, "class": t.cls.value, "kind": t.kind,
                   "writable": t.kind in ("holding", "coil") and t.cls != RegClass.diagnostic}
            if t.limits:
                row["limits"] = list(t.limits)
            out.append(row)
        return out

    def _tag(self, name: str) -> TagInfo:
        t = self.world.tags.get(name)
        if t is None or not self._visible(t):
            raise ToolError(f"unknown tag {name!r}; call list_tags")
        return t

    # -- tools
    async def read_tag(self, tag: str) -> dict:
        t = self._tag(tag)
        res = await self._read(t.kind, int(t.address), 1)
        if "values" in res:
            res["value"] = res.pop("values")[0] / (t.scale or 1.0)
        return res

    async def write_tag(self, tag: str, value: float) -> dict:
        t = self._tag(tag)
        if t.kind not in MAX_WRITE:
            raise ToolError(f"{tag} is a read-only {t.kind} register")
        raw = round(float(value) * (t.scale or 1.0))
        if t.kind == "coil":
            if raw not in (0, 1):
                raise ToolError("a coil takes 0 or 1")
        elif not 0 <= raw <= 0xFFFF:
            raise ToolError(f"{value} does not fit a 16-bit register at scale {t.scale}")
        return await self._write(t.kind, int(t.address), [raw])

    async def read_registers(self, kind: str, address: int, count: int = 1) -> dict:
        if not isinstance(kind, str) or kind not in READ_FC:
            raise ToolError(f"kind must be one of {sorted(READ_FC)}")
        self._check_range(address, count, MAX_READ[kind])
        return await self._read(kind, address, count)

    async def write_registers(self, kind: str, address: int, values: list[int]) -> dict:
        if not isinstance(kind, str) or kind not in MAX_WRITE:
            raise ToolError("only holding registers and coils are writable")
        if not isinstance(values, list) or not values:
            raise ToolError("values must be a non-empty list")
        self._check_range(address, len(values), MAX_WRITE[kind])
        hi = 1 if kind == "coil" else 0xFFFF
        if any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= hi for v in values):
            raise ToolError(f"each value must be an integer 0..{hi}")
        return await self._write(kind, address, values)

    def status(self) -> dict:
        st = self.gate.registry.state(self.agent_id)
        return {"agent": self.agent_id, "role": st.role, "tier": st.tier.value, "registered": st.registered}

    @staticmethod
    def _check_range(address: int, count: int, max_count: int):
        if not isinstance(address, int) or isinstance(address, bool) or not 0 <= address <= 0xFFFF:
            raise ToolError("address must be an integer 0..65535")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= max_count:
            raise ToolError(f"count must be 1..{max_count}")
        if address + count > 0x10000:
            raise ToolError("range runs past address 65535")

    # -- judging, same rules as the proxy's frame handling
    def _tag_scale(self, kind: str, addr: int) -> tuple[str, float]:
        t = self.world.tag_at(kind, addr)
        return (t.tag, (t.scale or 1.0)) if t else (f"addr:{addr}", 1.0)

    def _request(self, kind: str, addr: int, op: Op, raw: float | None, count: int) -> Request:
        t = self.world.tag_at(kind, addr)
        value = raw
        if t is None:
            target = Target(tag=f"addr:{addr}", cls=RegClass.unknown)
        else:
            target = Target(tag=t.tag, cls=t.cls, writable_while_running=t.writable_while_running,
                            safe_direction=t.safe_direction, is_trap=t.is_trap)
            if value is not None and t.scale and t.scale != 1.0:
                value = value / t.scale       # judge in engineering units, like the proxy
        return Request(agent_id=self.agent_id, protocol=Protocol.modbus, op=op, target=target, value=value, count=count)

    async def _decide(self, reqs: list[Request]) -> tuple[Any, list]:
        now = self.gate.clock()
        plant = self.plant_state_fn()
        decisions = [await self.gate.decide_async(r, plant, source=None, now=now) for r in reqs]
        return max(decisions, key=lambda d: STRICTNESS[d.action]), decisions

    def _verdict(self, d, extra: dict | None = None) -> dict:
        out = {"ok": d.action in (Action.allow, Action.cache), "action": d.action.value, "tier": d.tier.value,
               "ledger_id": d.ledger_id}
        if d.action == Action.defer and d.retry_after_ms:
            out["retry_after_ms"] = int(d.retry_after_ms)
        if self.explain:
            out["reasons"] = list(d.reasons)
        if extra:
            out.update(extra)
        return out

    def _refused(self, strictest, decisions) -> dict | None:
        if any(d.tier == Tier.blocked for d in decisions):
            self.counts["denied"] += 1
            res = self._verdict(strictest)
            res.update(ok=False, action="deny", tier="blocked")
            return res
        if strictest.action == Action.deny:
            self.counts["denied"] += 1
            return self._verdict(strictest)
        if strictest.action == Action.defer:
            self.counts["deferred"] += 1
            return self._verdict(strictest)
        return None

    async def _forward(self, pdu: bytes, reqs: list[Request], strictest) -> bytes | dict:
        try:
            resp = await self.client.request(self.unit, pdu)
        except UpstreamError as e:
            self.counts["upstream_errors"] += 1
            raise ToolError(f"controller unreachable: {e}") from e
        self.counts["forwarded"] += 1
        if resp and resp[0] & 0x80:
            now = self.gate.clock()
            for r in reqs:
                self.gate.registry.state(r.agent_id).errors.appendleft(now)
            res = self._verdict(strictest, {"controller_exception": resp[1] if len(resp) > 1 else None})
            res["ok"] = False
            return res
        return resp

    def _bookkeep(self, fn) -> None:
        """Value-cache / fingerprint updates after the controller answered. A bug here must never turn a request the
        controller already executed into a failure for the agent (same rule as the proxy)."""
        try:
            fn(self.gate.clock())
        except Exception as e:  # noqa: BLE001
            self.counts["errors"] += 1
            if self.counts["errors"] <= 5:
                print(f"[talos-mcp] bookkeeping error after forward: {type(e).__name__}: {e}", file=sys.stderr)

    async def _read(self, kind: str, address: int, count: int) -> dict:
        self.counts["calls"] += 1
        req = self._request(kind, address, Op.read, None, count)
        strictest, decisions = await self._decide([req])
        refused = self._refused(strictest, decisions)
        if refused:
            return refused
        if strictest.action == Action.cache:
            now = self.gate.clock()
            vals = []
            for i in range(count):
                tg, sc = self._tag_scale(kind, address + i)
                f = self.gate.values.fresh(tg, now)
                if f is None:
                    vals = None; break
                vals.append(f[0] * sc)
            if vals is not None:
                self.counts["served_cache"] += 1
                return self._verdict(strictest, {"values": [_num(v) for v in vals], "source": "cache"})
        resp = await self._forward(read_pdu(kind, address, count), [req], strictest)
        if isinstance(resp, dict):
            return resp
        vals = parse_read_response(READ_FC[kind], resp, count)
        if vals is None:
            raise ToolError("controller sent a malformed read response")
        vals = vals[:count]

        def observe(now):
            for i, v in enumerate(vals):
                tg, sc = self._tag_scale(kind, address + i)
                self.gate.observe_read(tg, v / sc, now)
        self._bookkeep(observe)
        res = self._verdict(strictest, {"values": [_num(v) for v in vals], "source": "controller"})
        res["ok"] = True
        return res

    async def _write(self, kind: str, address: int, values: list[int]) -> dict:
        self.counts["calls"] += 1
        reqs = []
        for i, v in enumerate(values):
            r = self._request(kind, address + i, Op.write, float(v), 1)
            r.budget_charge = i == 0           # one frame, one token, judged per register (same as the proxy)
            reqs.append(r)
        strictest, decisions = await self._decide(reqs)
        refused = self._refused(strictest, decisions)
        if refused:
            return refused
        if strictest.action == Action.cache:
            self.counts["served_cache"] += 1    # the controller already holds these values
            return self._verdict(strictest, {"written": 0})
        resp = await self._forward(write_pdu(kind, address, values), reqs, strictest)
        if isinstance(resp, dict):
            return resp

        def observe(now):
            for i, v in enumerate(values):
                tg, sc = self._tag_scale(kind, address + i)
                self.gate.observe_write(tg, v / sc, now, agent_id=self.agent_id)
        self._bookkeep(observe)
        res = self._verdict(strictest, {"written": len(values)})
        res["ok"] = True
        return res


def _num(v: float) -> float | int:
    return int(v) if float(v).is_integer() else v


# --------------------------------------------------------------------------------------------------- MCP protocol
_KIND = {"type": "string", "enum": sorted(READ_FC)}
_ADDR = {"type": "integer", "minimum": 0, "maximum": 65535}
TOOLS = [
    {"name": "list_tags", "description": "List the plant tags this agent may read or write, with class, kind and limits.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "read_tag", "description": "Read one tag from list_tags, in engineering units.",
     "inputSchema": {"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"],
                     "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "write_tag", "description": "Write one tag from list_tags, in engineering units. The gate may allow, "
                                         "defer (retry after retry_after_ms) or deny the write.",
     "inputSchema": {"type": "object", "properties": {"tag": {"type": "string"}, "value": {"type": "number"}},
                     "required": ["tag", "value"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
    {"name": "read_registers", "description": "Raw Modbus read: kind, start address and count. Values are raw.",
     "inputSchema": {"type": "object", "properties": {"kind": _KIND, "address": _ADDR,
                                                       "count": {"type": "integer", "minimum": 1, "maximum": 2000}},
                     "required": ["kind", "address"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "write_registers", "description": "Raw Modbus write to holding registers or coils starting at address. "
                                               "Values are raw (0..65535, coils 0/1).",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["coil", "holding"]}, "address": _ADDR,
         "values": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 65535}, "minItems": 1}},
         "required": ["kind", "address", "values"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
    {"name": "status", "description": "This agent's id, role and tier.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
]
_REQUIRED = {t["name"]: (set(t["inputSchema"].get("required", [])), set(t["inputSchema"]["properties"])) for t in TOOLS}


class McpServer:
    """JSON-RPC 2.0 dispatch for the MCP methods a tool server needs."""

    def __init__(self, gw: McpGateway, name: str = "talos", version: str = "0.1.0"):
        self.gw = gw
        self.info = {"name": name, "version": version}

    async def handle(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return None                  # a response to something we never send; ignore
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if "id" not in msg:
            return None                      # notifications (initialized, cancelled, ...) need no answer
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
                return _result(mid, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                     "serverInfo": self.info,
                                     "instructions": "Plant access goes through the Talos gate. Use list_tags first; "
                                                     "back off when a call returns action=defer."})
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": TOOLS})
            if method == "tools/call":
                return _result(mid, await self._call(params))
            return _error(mid, -32601, f"method not found: {method}")
        except Exception as e:  # noqa: BLE001 - a gateway bug answers this request with an error; the server keeps going
            print(f"[talos-mcp] error in {method}: {type(e).__name__}: {e}", file=sys.stderr)
            return _error(mid, -32603, "internal error")

    async def _call(self, params: dict) -> dict:
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in _REQUIRED:
            return _tool_error(f"unknown tool {name!r}")
        required, allowed = _REQUIRED[name]
        if not isinstance(args, dict) or not required <= set(args) or not set(args) <= allowed:
            return _tool_error(f"{name} takes {sorted(allowed)} (required {sorted(required)})")
        gw = self.gw
        try:
            if name == "list_tags":
                out: Any = {"tags": gw.list_tags()}
            elif name == "read_tag":
                out = await gw.read_tag(_str(args["tag"]))
            elif name == "write_tag":
                out = await gw.write_tag(_str(args["tag"]), _number(args["value"]))
            elif name == "read_registers":
                out = await gw.read_registers(args["kind"], args["address"], args.get("count", 1))
            elif name == "write_registers":
                out = await gw.write_registers(args["kind"], args["address"], args["values"])
            else:
                out = gw.status()
        except ToolError as e:
            return _tool_error(str(e))
        is_error = isinstance(out, dict) and out.get("ok") is False
        return {"content": [{"type": "text", "text": orjson.dumps(out).decode()}], "structuredContent": out,
                "isError": is_error}

    async def serve(self, reader: asyncio.StreamReader, write) -> None:
        """Newline-delimited JSON-RPC, one message per line (the MCP stdio transport)."""
        while True:
            line = await reader.readline()
            if not line:
                return
            if not line.strip():
                continue
            try:
                msg = orjson.loads(line)
            except orjson.JSONDecodeError:
                write(_error(None, -32700, "parse error"))
                continue
            if isinstance(msg, list):         # batches (2025-03-26 only)
                replies = [r for r in [await self.handle(m) for m in msg] if r is not None]
                if replies:
                    write(replies)
                continue
            reply = await self.handle(msg)
            if reply is not None:
                write(reply)


def _str(v) -> str:
    if not isinstance(v, str):
        raise ToolError("tag must be a string")
    return v


def _number(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ToolError("value must be a number")
    return float(v)


def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ------------------------------------------------------------------------------------------------------------ CLI
def parse_agent(spec: str | None) -> tuple[str, str] | None:
    """`ID[:ROLE]` -> (id, role)."""
    if not spec:
        return None
    agent_id, _, role = spec.partition(":")
    role = role or "unknown"
    if not agent_id or role not in ROLES:
        raise SystemExit(f"--agent wants ID[:ROLE] with ROLE in {sorted(ROLES)}, got {spec!r}")
    return agent_id, role


def build(args) -> tuple[McpServer, Gate]:
    world = World.load(args.world) if args.world else World()
    cfg = GateConfig(stop_bypass=not args.no_stop_bypass, model_deadline_ms=args.deadline_ms, shadow=args.shadow)
    spec = model_spec(args.model, args.weights)
    judge = make_judge(spec)
    if hasattr(judge, "judge_tokens"):
        judge.judge_tokens(["OP=read"])          # warm the ONNX session before the first real call
    gate = Gate(cfg, world=world, judge=judge, ledger=Ledger(args.ledger))
    agent = parse_agent(args.agent)
    if agent:
        gate.registry.register(agent[0], role=agent[1], tier=Tier.normal)
        agent_id = agent[0]
    else:
        agent_id = gate.registry.resolve("mcp", {"client_id": ""})    # unregistered: small budget, like the proxy
    host, port = args.upstream.rsplit(":", 1)
    gw = McpGateway(gate, world, ModbusClient(host, int(port)), agent_id, unit=args.unit, explain=args.explain)
    print(f"[talos-mcp] agent={agent_id} upstream={args.upstream} unit={args.unit} "
          f"world={world.plant_id if world.tags else 'BLIND'} model={spec} shadow={args.shadow}", file=sys.stderr)
    return McpServer(gw), gate


async def serve_stdio(server: McpServer) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout.buffer

    def write(obj):
        out.write(orjson.dumps(obj) + b"\n")
        out.flush()

    await server.serve(reader, write)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="talos-mcp", description="MCP server that puts the Talos gate between an AI agent "
                                                               "and a Modbus TCP controller (stdio transport).")
    ap.add_argument("--upstream", default="127.0.0.1:15020", help="the controller, host:port (default 127.0.0.1:15020)")
    ap.add_argument("--unit", type=int, default=1, help="Modbus unit id to address (default 1)")
    ap.add_argument("--agent", default=None, metavar="ID[:ROLE]", help="who this server speaks for, e.g. copilot:copilot. "
                    "Omitted: an unregistered agent with a small budget")
    ap.add_argument("--world", default=None, help="tag map (world.json); omit for blind mode (raw register tools only)")
    ap.add_argument("--ledger", default="talos-ledger.jsonl")
    ap.add_argument("--model", default=None, help="null | systemone:URL | onnx:PATH (default: auto, see --weights)")
    ap.add_argument("--weights", default=None, metavar="PATH")
    ap.add_argument("--shadow", action="store_true", help="judge and log, never block")
    ap.add_argument("--no-stop-bypass", action="store_true")
    ap.add_argument("--deadline-ms", type=float, default=20.0)
    ap.add_argument("--explain", action="store_true", help="include the gate's reasons in tool results (debugging only)")
    args = ap.parse_args(argv)
    server, gate = build(args)
    try:
        asyncio.run(serve_stdio(server))
    except KeyboardInterrupt:
        pass
    finally:
        c = server.gw.counts
        print(f"[talos-mcp] calls={c['calls']} forwarded={c['forwarded']} cache={c['served_cache']} denied={c['denied']} "
              f"deferred={c['deferred']} upstream_errors={c['upstream_errors']} | gate by={gate.stats['by']}", file=sys.stderr)
        gate.ledger.close()


if __name__ == "__main__":
    main()
