# The MCP front end

An MCP server for AI agents that need to read or write a Modbus TCP controller. Every tool call becomes one Modbus frame and goes through the same gate as the proxy before anything reaches the PLC. Standard library only: the server speaks MCP's stdio transport (newline-delimited JSON-RPC 2.0) itself, so no MCP SDK is needed on the gateway.

```bash
talos-mcp --upstream 192.0.2.10:502 --world world.json --weights weights/ --agent copilot:copilot
```

The agent's MCP client launches that command (it is a stdio server). For example, in a client config:

```json
{"mcpServers": {"plant": {"command": "talos-mcp",
  "args": ["--upstream", "192.0.2.10:502", "--world", "world.json", "--weights", "weights", "--agent", "copilot:copilot"]}}}
```

## Tools

| tool | arguments | what it does |
|---|---|---|
| `list_tags` | | the tags in the map, with asset, class, kind, limits and whether they are writable. Trap registers are never listed. |
| `read_tag` | `tag` | read one mapped tag, in engineering units |
| `write_tag` | `tag`, `value` | write one mapped tag, in engineering units (raw = value x scale) |
| `read_registers` | `kind`, `address`, `count` | raw read of holding / input registers, coils or discrete inputs. The only read in blind mode. |
| `write_registers` | `kind`, `address`, `values` | raw write to holding registers or coils, one frame |
| `status` | | this agent's id, role and tier |

Every result carries `action` (`allow`, `cache`, `defer`, `deny`), `tier` and the `ledger_id` of the decision. A refused call is an MCP tool error (`isError: true`) so the model sees it.

| decision | the agent gets |
|---|---|
| allow | the values, or `written: N` |
| cache | values from the gate's cache (`source: cache`), or a duplicate write acknowledged with `written: 0`; the PLC is not touched |
| defer | `retry_after_ms`: back off and retry |
| deny | refused |
| blocked tier | refused, and every later call too |
| PLC exception | `controller_exception` with the Modbus code; counts as an error against the agent |

The agent is told what happened, not why. Telling a misbehaving client which rule it tripped (a trap, a budget, a clamp) helps it walk around the rule. `--explain` adds the gate's `reasons` for debugging.

Malformed calls (an unknown tag, a bad address range, a value that does not fit a register) are answered with a tool error before the gate is consulted and never reach the PLC.

## Same rules as the proxy

A call is judged exactly like the Modbus frame it turns into: a block write is judged per register and charged one budget token, a block read is judged once, the strictest decision wins, reads are served from the value cache when fresh, duplicate writes are acknowledged without a write, and the value cache and fingerprints are updated from what the controller returns. Unmapped addresses use the same `addr:<address>` key, so blind mode and the model see the same thing they see behind the proxy.

## Identity

One server process speaks for one agent: whatever `--agent ID[:ROLE]` the operator put on the command line. Nothing the agent sends (client name, tool arguments) is used as identity. Without `--agent` the server runs as an unregistered agent with the small budget an unknown Modbus source gets. Roles: `copilot`, `optimizer`, `dashboard`, `historian`, `hmi`, `engineering`, `unknown`.

## Flags

| flag | default | meaning |
|---|---|---|
| `--upstream HOST:PORT` | `127.0.0.1:15020` | the controller |
| `--unit N` | `1` | Modbus unit id to address |
| `--agent ID[:ROLE]` | unregistered | who this server speaks for |
| `--world PATH` | none (blind mode) | the map, see `WORLD-FORMAT.md` |
| `--ledger PATH` | `talos-ledger.jsonl` | the record, see `LEDGER.md` |
| `--weights PATH` / `--model SPEC` | as the proxy | the judge |
| `--shadow` | off | judge and log, never block |
| `--no-stop-bypass` | off | stops and safe-direction writes also go through the model |
| `--deadline-ms N` | `20` | model deadline |
| `--explain` | off | include the gate's reasons in tool results |

Protocol revisions 2024-11-05 through 2025-11-25 (the `initialize` handshake). Not yet: the streamable HTTP transport. Over HTTP many agents share one server, so it needs per-agent credentials (a bearer token per agent) before it can keep identities apart; that is the next step.

Code: `talos/gateway/mcp_server.py`.
