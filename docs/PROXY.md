# The Modbus TCP proxy

A transparent proxy in front of a Modbus TCP controller. Clients point at the proxy instead of the PLC. Standard library only; no PLC driver needed.

```bash
talos-proxy --upstream 192.0.2.10:502 --listen 15502 --weights weights/
```

Clients then connect to `<proxy host>:15502`. Nothing changes on the PLC.

## Flags

| flag | default | meaning |
|---|---|---|
| `--upstream HOST:PORT` | `127.0.0.1:15020` | the controller |
| `--listen PORT` | `15502` | where clients connect |
| `--weights PATH` | `$TALOS_WEIGHTS`, else `./weights` if present | directory with `student.int8.onnx`, `vocab.json`, `heads.json`. Absent: rules only, with a one-line notice. |
| `--model SPEC` | | alternative to `--weights`: `null`, `onnx:PATH`, or `systemone:URL` for a remote System One judge |
| `--world PATH` | none (blind mode) | the map, see `WORLD-FORMAT.md` |
| `--ledger PATH` | `talos-ledger.jsonl` | the record, see `LEDGER.md` |
| `--shadow` | off | judge and log, never block |
| `--no-stop-bypass` | off | stops and safe-direction writes also go through the model |
| `--deadline-ms N` | `20` | model deadline; on timeout the rules decide and the client stays connected. Use 100 to 200 on a small CPU such as a Jetson or Pi |
| `--deny-exception N` | `4` | Modbus exception code returned on deny: `4` (server device failure) or `2` (illegal data address) |
| `--report-every N` | `0` | print a decision summary every N requests |
| `--agent ID=IP:UNIT[:ROLE]` | | register a client (repeatable). Registered agents get the normal budget and can earn `trusted`; unregistered sources get a small budget. |

## What clients see

| decision | on the wire |
|---|---|
| allow | forwarded to the PLC, response relayed |
| cache | answered from the proxy's cache, PLC not touched |
| defer | Modbus exception `0x06` (server device busy); retry after the budget refills |
| deny | exception `0x04` by default (or `0x02` with `--deny-exception 2`) |
| blocked tier | connection closed |
| unknown function code | exception `0x01` |

Function codes handled: 1, 2, 3, 4 (reads), 5, 6, 15, 16 (writes), 23 (read/write). Everything else is denied with `0x01`.

## Identity on Modbus

Modbus has no authentication. The proxy keys identity on the client's source address and unit id. That is enough to keep reputation per client and to rank bad ones out; it is not an authorization credential. See `IDENTITY.md` and `THREAT-MODEL.md`.

## Where to run it

- On the host the agents run on (simplest; protects one host's traffic).
- On a gateway or industrial PC between the agent network and the control network (protects everything that crosses).
- Blind mode needs no configuration at all: start it, point clients at it, read the ledger.

## As a library

```python
from talos.gate import Gate, GateConfig
from talos.world import World
from talos.model.onnx import OnnxJudge
from talos.schema import Request, Target, Op, RegClass, Protocol

gate = Gate(GateConfig(), world=World.load("world.json"), judge=OnnxJudge("weights/"))
dec = gate.decide(Request(agent_id="a:copilot", protocol=Protocol.modbus, op=Op.write,
                          target=Target(tag="t:pump1.speed_ref", cls=RegClass.setpoint), value=5800))
print(dec.action, dec.by, dec.reasons)
```

Anything that can produce a `Request` and a `Context` can use the gate: an OPC UA gateway, an MCP server, an HTTP front end. The schema is in `SCHEMA.md`.

Code: `talos/gateway/modbus_proxy.py`.
