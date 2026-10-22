# Agent identity

Reputation needs a subject. Talos attributes every request to an `agent_id` and keeps the reputation, tier, budgets, and history under it.

## Where the id comes from

| transport | identity source | strength |
|---|---|---|
| MCP server plugin | MCP client id / session, optionally a signed token in the request metadata | strong |
| OPC UA | session name + application URI + client certificate thumbprint when security is on | strong with certs, weak without |
| Modbus TCP proxy | (source IP, source port range, unit id) + optional registration via a side channel (gateway API) | weak; Modbus has no auth |
| HTTP/REST gateway | bearer token / API key | strong |

Unregistered sources get a synthetic id `u:<hash(src, unit)>` and run in `normal` with a reduced budget (20 reads/min, 3 writes/min). Registration (an operator naming the agent and its role) gives it the normal budget and makes promotion to `trusted` possible. This matches the May 2026 Five Eyes agentic guidance: dedicated identity per agent, least privilege.

## What is kept per id

- role (copilot, optimizer, dashboard, historian, hmi, engineering, unknown)
- tier (trusted / normal / quarantine / blocked) with entry time and hysteresis counters
- reputation: an EWMA of per-request outcomes (allowed clean = +, deferred = 0, denied = -, trap = --)
- budgets: token buckets per op class
- bounded recent history (last N requests, typed rows)
- per-register last-read / last-write for contradiction and duplicate checks

Nothing free-text. The agent's name is interned to an id before rendering.

## Spoofing

Identity on raw Modbus is spoofable. Talos treats it as a feature for reputation, not as authorization; the allowlist is by tier and the hard clamps hold regardless of identity. Behaviour, not the name, is what moves a tier.
