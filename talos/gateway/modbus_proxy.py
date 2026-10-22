# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Reference gateway plugin: a raw-socket Modbus TCP proxy with Talos in the write path.

    talos-proxy --listen 15502 --upstream 127.0.0.1:15020 --world examples/world.json --weights weights/ \
        [--ledger talos-ledger.jsonl] [--shadow] [--model null|systemone:URL|onnx:PATH] [--deny-exception 4] [--no-stop-bypass]

(`python -m talos.gateway.modbus_proxy` is the same entry point.) Without --weights/--model the proxy looks for weights in
$TALOS_WEIGHTS, then ./weights; if neither exists it runs the deterministic fast path only (null judge) and says so.

Reads are forwarded untouched unless every register is fresh in the value cache (then served locally).
Writes are held for Gate.decide: deny -> Modbus exception (default 0x04 slave device failure), defer -> 0x06 (busy),
blocked tier -> connection closed. Unknown function codes -> exception 0x01 and a deny in the ledger.
Added latency per request is measured and printed on exit (and every --report-every requests)."""
from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time

from talos import weights as _weights
from talos.gate import Gate, GateConfig
from talos.ledger import Ledger
from talos.model import NullJudge, OnnxJudge, SystemOneJudge
from talos.schema import Action, Op, Protocol, RegClass, Request, Target, Tier
from talos.world import PlantState, World

FC_KIND = {1: "coil", 2: "discrete", 3: "holding", 4: "input", 5: "coil", 6: "holding", 15: "coil", 16: "holding", 23: "holding"}
READ_FC = {1, 2, 3, 4}
WRITE_FC = {5, 6, 15, 16}
EXC_ILLEGAL_FUNCTION, EXC_ILLEGAL_ADDRESS, EXC_DEVICE_FAILURE, EXC_BUSY = 0x01, 0x02, 0x04, 0x06


def parse_pdu(pdu: bytes) -> dict | None:
    """-> {"fc", "reads": [(kind, addr, qty)], "writes": [(kind, addr, value)]} or None if malformed."""
    if not pdu:
        return None
    fc = pdu[0]
    try:
        if fc in READ_FC:
            addr, qty = struct.unpack(">HH", pdu[1:5])
            return {"fc": fc, "reads": [(FC_KIND[fc], addr, qty)], "writes": []}
        if fc == 5:
            addr, val = struct.unpack(">HH", pdu[1:5])
            return {"fc": fc, "reads": [], "writes": [("coil", addr, 1.0 if val == 0xFF00 else 0.0)]}
        if fc == 6:
            addr, val = struct.unpack(">HH", pdu[1:5])
            return {"fc": fc, "reads": [], "writes": [("holding", addr, float(val))]}
        if fc == 15:
            addr, qty, bc = struct.unpack(">HHB", pdu[1:6])
            bits = pdu[6:6 + bc]
            vals = [(bits[i // 8] >> (i % 8)) & 1 for i in range(qty)]
            return {"fc": fc, "reads": [], "writes": [("coil", addr + i, float(v)) for i, v in enumerate(vals)]}
        if fc == 16:
            addr, qty, bc = struct.unpack(">HHB", pdu[1:6])
            vals = struct.unpack(f">{qty}H", pdu[6:6 + 2 * qty])
            return {"fc": fc, "reads": [], "writes": [("holding", addr + i, float(v)) for i, v in enumerate(vals)]}
        if fc == 23:
            raddr, rqty, waddr, wqty, bc = struct.unpack(">HHHHB", pdu[1:10])
            vals = struct.unpack(f">{wqty}H", pdu[10:10 + 2 * wqty])
            return {"fc": fc, "reads": [("holding", raddr, rqty)], "writes": [("holding", waddr + i, float(v)) for i, v in enumerate(vals)]}
    except struct.error:
        return None
    return {"fc": fc, "reads": [], "writes": [], "unknown": True}


def exception_frame(tid: int, uid: int, fc: int, code: int) -> bytes:
    pdu = bytes([fc | 0x80, code])
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu


def build_read_response(fc: int, values: list[float]) -> bytes:
    if fc in (1, 2):
        n = (len(values) + 7) // 8
        b = bytearray(n)
        for i, v in enumerate(values):
            if v:
                b[i // 8] |= 1 << (i % 8)
        return bytes([fc, n]) + bytes(b)
    data = b"".join(struct.pack(">H", int(v) & 0xFFFF) for v in values)
    return bytes([fc, len(data)]) + data


def parse_read_response(fc: int, pdu: bytes, qty: int) -> list[float] | None:
    if len(pdu) < 2 or pdu[0] != fc:
        return None
    bc = pdu[1]
    data = pdu[2:2 + bc]
    if fc in (1, 2):
        return [float((data[i // 8] >> (i % 8)) & 1) for i in range(qty)]
    n = bc // 2
    return [float(x) for x in struct.unpack(f">{n}H", data[:2 * n])]


class ModbusProxy:
    def __init__(self, gate: Gate, upstream: tuple[str, int], world: World, deny_exception: int = EXC_DEVICE_FAILURE,
                 report_every: int = 0, plant_state_fn=None):
        self.gate = gate
        self.upstream = upstream
        self.world = world
        self.deny_exception = deny_exception
        self.report_every = report_every
        self.plant_state_fn = plant_state_fn or (lambda: PlantState())
        self.latencies: list[float] = []     # proxy-added ms per request (decision + bookkeeping, excluding upstream RTT)
        self.total: list[float] = []         # end-to-end ms per request
        self.counts = {"requests": 0, "served_cache": 0, "forwarded": 0, "denied": 0, "deferred": 0, "closed": 0, "unknown_fc": 0, "errors": 0}
        self.asset_state_fn = None

    @staticmethod
    def unmapped_tag(addr: int) -> str:
        """Tag name for an address the map does not know: `addr:<address>`, the same key the training pipeline uses, so a
        blind gate's fingerprints line up with what the model saw in training. (Coils and registers share the namespace;
        the proxy assumes a device's address space is disjoint across kinds.)"""
        return f"addr:{addr}"

    def _tag_scale(self, kind: str, addr: int) -> tuple[str, float]:
        t = self.world.tag_at(kind, addr)
        return (t.tag, (t.scale or 1.0)) if t else (self.unmapped_tag(addr), 1.0)

    def _request_for(self, kind: str, addr: int, op: Op, value: float | None, count: int, src: dict) -> Request:
        t = self.world.tag_at(kind, addr)
        if t is None:
            tag = self.unmapped_tag(addr)
            target = Target(tag=tag, cls=RegClass.unknown)
        else:
            target = Target(tag=t.tag, cls=t.cls, writable_while_running=t.writable_while_running, safe_direction=t.safe_direction, is_trap=t.is_trap)
            if value is not None and t.scale and t.scale != 1.0:
                value = value / t.scale          # raw = eng * scale (the world's convention): judge in engineering units
        return Request(agent_id="", protocol=Protocol.modbus, op=op, target=target, value=value, count=count)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername") or ("?", 0)
        src_ip = peer[0]
        try:
            up_r, up_w = await asyncio.open_connection(*self.upstream)
        except OSError:
            writer.close(); return
        try:
            while True:
                hdr = await reader.readexactly(7)
                tid, pid, length, uid = struct.unpack(">HHHB", hdr)
                pdu = await reader.readexactly(length - 1)
                t0 = time.perf_counter()
                try:
                    resp, close = await self._process(tid, uid, pdu, {"ip": src_ip, "unit": uid}, up_r, up_w, t0)
                except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                    raise   # the controller side went away: nothing to answer with
                except Exception as e:  # noqa: BLE001 - never drop a client over a gate bug; answer this frame and go on
                    self.counts["errors"] += 1
                    if self.counts["errors"] <= 5:
                        print(f"[talos-proxy] error judging frame from {src_ip}: {type(e).__name__}: {e}", file=sys.stderr)
                    resp, close = exception_frame(tid, uid, pdu[0] if pdu else 0, self.deny_exception), False
                if resp:
                    writer.write(resp)
                    await writer.drain()
                self.total.append((time.perf_counter() - t0) * 1000)
                self.counts["requests"] += 1
                if self.report_every and self.counts["requests"] % self.report_every == 0:
                    self.report()
                if close:
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close(); up_w.close()

    async def _forward(self, tid: int, uid: int, pdu: bytes, up_r, up_w) -> bytes:
        up_w.write(struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu)
        await up_w.drain()
        hdr = await up_r.readexactly(7)
        _, _, length, _ = struct.unpack(">HHHB", hdr)
        body = await up_r.readexactly(length - 1)
        return hdr + body

    async def _process(self, tid, uid, pdu, src, up_r, up_w, t0) -> tuple[bytes, bool]:
        parsed = parse_pdu(pdu)
        now = self.gate.clock()
        plant = self.plant_state_fn()
        if parsed is None or parsed.get("unknown"):
            fc = pdu[0] if pdu else 0
            self.counts["unknown_fc"] += 1
            req = Request(agent_id="", protocol=Protocol.modbus, op=Op.call, target=Target(tag=f"fc:{fc}", cls=RegClass.unknown))
            await self.gate.decide_async(req, plant, source=src, now=now)
            self.latencies.append((time.perf_counter() - t0) * 1000)
            return exception_frame(tid, uid, fc, EXC_ILLEGAL_FUNCTION), False
        fc = parsed["fc"]
        decisions = []
        # writes first (a FC23 read+write is judged as a write)
        for kind, addr, val in parsed["writes"]:
            req = self._request_for(kind, addr, Op.write, val, 1, src)
            decisions.append((req, await self.gate.decide_async(req, plant, source=src, now=now)))
        for kind, addr, qty in parsed["reads"]:
            req = self._request_for(kind, addr, Op.read, None, qty, src)
            decisions.append((req, await self.gate.decide_async(req, plant, source=src, now=now)))
        strictest = max((d for _, d in decisions), key=lambda d: {"allow": 0, "cache": 1, "defer": 2, "deny": 3}[d.action.value], default=None)
        self.latencies.append((time.perf_counter() - t0) * 1000)
        if strictest is None:
            return exception_frame(tid, uid, fc, EXC_ILLEGAL_FUNCTION), False
        if any(d.tier == Tier.blocked for _, d in decisions):
            self.counts["closed"] += 1
            return exception_frame(tid, uid, fc, self.deny_exception), True
        if strictest.action == Action.deny:
            self.counts["denied"] += 1
            return exception_frame(tid, uid, fc, self.deny_exception), False
        if strictest.action == Action.defer:
            self.counts["deferred"] += 1
            return exception_frame(tid, uid, fc, EXC_BUSY), False
        if fc in READ_FC and strictest.action == Action.cache:
            kind, addr, qty = parsed["reads"][0]
            vals = []
            for i in range(qty):
                tg, sc = self._tag_scale(kind, addr + i)
                f = self.gate.values.fresh(tg, now)
                if f is None:
                    vals = None; break
                vals.append(f[0] * sc)
            if vals is not None:
                self.counts["served_cache"] += 1
                rp = build_read_response(fc, vals)
                return struct.pack(">HHHB", tid, 0, len(rp) + 1, uid) + rp, False
        if fc in WRITE_FC and strictest.action == Action.cache:
            # duplicate write: acknowledge without touching the controller (echo request as response, like a device would)
            self.counts["served_cache"] += 1
            return struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu, False
        # forward
        resp = await self._forward(tid, uid, pdu, up_r, up_w)
        self.counts["forwarded"] += 1
        rpdu = resp[7:]
        if rpdu and not (rpdu[0] & 0x80):
            now2 = self.gate.clock()
            for kind, addr, qty in parsed["reads"]:
                vals = parse_read_response(fc if fc in READ_FC else 3, rpdu, qty)
                if vals:
                    for i, v in enumerate(vals):
                        tg, sc = self._tag_scale(kind, addr + i)
                        self.gate.observe_read(tg, v / sc, now2)
            writer = decisions[0][0].agent_id if decisions else None
            for kind, addr, val in parsed["writes"]:
                tg, sc = self._tag_scale(kind, addr)
                self.gate.observe_write(tg, val / sc, now2, agent_id=writer)
        else:
            for req, _ in decisions:
                st = self.gate.registry.state(req.agent_id)
                st.errors.appendleft(now)
        return resp, False

    def report(self, file=sys.stderr):
        if not self.total:
            print("no requests", file=file); return
        def pct(xs, p):
            xs = sorted(xs); return xs[min(len(xs) - 1, int(p * len(xs)))]
        print(f"[talos-proxy] n={self.counts['requests']} forwarded={self.counts['forwarded']} cache={self.counts['served_cache']} "
              f"denied={self.counts['denied']} deferred={self.counts['deferred']} closed={self.counts['closed']} unknown_fc={self.counts['unknown_fc']} | "
              f"added ms p50={pct(self.latencies, .5):.3f} p99={pct(self.latencies, .99):.3f} max={max(self.latencies):.3f} | "
              f"e2e ms p50={pct(self.total, .5):.3f} p99={pct(self.total, .99):.3f} | gate by={self.gate.stats['by']}", file=file)


def make_judge(spec: str):
    """`null` | `systemone:URL` | `onnx:PATH` (PATH = weights dir or .onnx file)."""
    if spec == "null":
        return NullJudge()
    if spec.startswith("systemone:"):
        return SystemOneJudge(spec.split(":", 1)[1])
    if spec.startswith("onnx:"):
        return OnnxJudge(spec.split(":", 1)[1])
    raise SystemExit(f"unknown --model {spec}")


def model_spec(model: str | None, weights: str | None) -> str:
    """Resolve the CLI's --model / --weights into a make_judge spec. --weights wins; an explicit --model is honoured as-is;
    with neither, weights come from $TALOS_WEIGHTS or ./weights, else the null judge (fast path only) with a notice."""
    if weights:
        return f"onnx:{weights}"
    if model:
        return model
    found = _weights.resolve(None)
    if found:
        return f"onnx:{found}"
    print("[talos-proxy] no weights found (pass --weights PATH, set TALOS_WEIGHTS, or run talos-pull): fast path only, no model",
          file=sys.stderr)
    return "null"


def parse_agent(spec: str) -> tuple[str, dict, str]:
    """`ID=IP:UNIT[:ROLE]` -> (agent_id, {"ip", "unit"}, role). Sources not registered this way get the small budget of an
    unknown agent (quarantine-sized reads, a few writes a minute) until they earn more."""
    try:
        agent_id, rest = spec.split("=", 1)
        ip, unit, *role = rest.split(":")
        return agent_id, {"ip": ip, "unit": int(unit)}, (role[0] if role else "unknown")
    except ValueError:
        raise SystemExit(f"--agent wants ID=IP:UNIT[:ROLE], got {spec!r}") from None


async def serve(args):
    world = World.load(args.world) if args.world else World()   # no --world = blind mode: every register is judged from its fingerprint
    cfg = GateConfig(stop_bypass=not args.no_stop_bypass, model_deadline_ms=args.deadline_ms, shadow=args.shadow)
    spec = model_spec(args.model, getattr(args, "weights", None))
    judge = make_judge(spec)
    if hasattr(judge, "judge_tokens"):
        judge.judge_tokens(["OP=read"])          # first ONNX call pays session warm-up; do it before traffic, not inside a deadline
    gate = Gate(cfg, world=world, judge=judge, ledger=Ledger(args.ledger))
    for agent_id, src, role in (parse_agent(a) for a in (getattr(args, "agent", None) or [])):
        gate.registry.register(agent_id, role=role, tier=Tier.normal)
        gate.registry.register_source("modbus", src, agent_id)
        print(f"[talos-proxy] agent {agent_id} = {src['ip']} unit {src['unit']} ({role})", file=sys.stderr)
    host, port = args.upstream.rsplit(":", 1)
    proxy = ModbusProxy(gate, (host, int(port)), world, deny_exception=args.deny_exception, report_every=args.report_every)
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", args.listen)
    print(f"[talos-proxy] listening on :{args.listen} -> {args.upstream} world={world.plant_id if world.tags else 'BLIND'} tags={len(world.tags)} traps={len(world.traps())} model={spec} shadow={args.shadow}", file=sys.stderr)
    try:
        async with server:
            await server.serve_forever()
    finally:
        proxy.report()
        gate.ledger.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="talos-proxy", description="Modbus TCP proxy with the Talos gate in the write path.")
    ap.add_argument("--listen", type=int, default=15502, help="port to listen on (default 15502)")
    ap.add_argument("--upstream", default="127.0.0.1:15020", help="the controller, host:port (default 127.0.0.1:15020)")
    ap.add_argument("--world", default=None, help="tag map (world.json); omit for blind / map-free mode")
    ap.add_argument("--ledger", default="talos-ledger.jsonl", help="append-only hash-chained decision log (default talos-ledger.jsonl)")
    ap.add_argument("--model", default=None, help="null | systemone:URL | onnx:PATH (default: auto, see --weights)")
    ap.add_argument("--weights", default=None, metavar="PATH", help="student weights dir (same as --model onnx:PATH); "
                    "default $TALOS_WEIGHTS, then ./weights, else null judge")
    ap.add_argument("--agent", action="append", metavar="ID=IP:UNIT[:ROLE]", help="register a known Modbus source (repeatable), "
                    "e.g. --agent copilot=10.0.0.9:1:copilot; unregistered sources run on an unknown agent's small budget")
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--no-stop-bypass", action="store_true")
    ap.add_argument("--deadline-ms", type=float, default=20.0)
    ap.add_argument("--deny-exception", type=int, default=EXC_DEVICE_FAILURE, choices=[EXC_ILLEGAL_ADDRESS, EXC_DEVICE_FAILURE])
    ap.add_argument("--report-every", type=int, default=0)
    args = ap.parse_args(argv)
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
