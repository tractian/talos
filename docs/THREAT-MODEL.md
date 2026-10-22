# Threat model

What Talos is for, what it assumes, and what it does not do. Read this before putting it in front of anything that moves.

## What it is

A decision gate in the request path between clients (AI agents, scripts, dashboards, operator screens) and controllers. It judges every request, serves what it can from cache, defers what should wait, denies what should not land, ranks clients into tiers by their behaviour, and writes a hash-chained record of all of it.

## What it assumes

- **It sits inline.** Traffic that reaches the controller by another path is not judged. Put it on the switch, the gateway, or the host the agents run on, and close the other paths.
- **Identity is a feature, not a credential.** On raw Modbus the source address can be spoofed. Talos uses identity to keep reputation; authorization comes from behaviour, tier budgets, and the hard rules, which hold regardless of who claims to be sending.
- **The map may be wrong or absent.** Blind mode learns registers from traffic. A wrong limit in the map is a wrong clamp; keep maps small and honest.
- **The model can be absent.** Every hard rule, the budgets, the traps and the ledger run with no model at all. The model only ever makes a decision stricter.

## What it defends against

| threat | mechanism |
|---|---|
| Flooding, polling storms, retry loops | per-tier token buckets, flood rule, cache serving, quarantine |
| Address enumeration, scanning, guessing | trap registers, unknown-address handling |
| A write to a value the register has never held | envelope and fingerprint reasoning, cumulative-delta cap, shock detection |
| Slow ramps that stay inside limits | drift accumulator, cumulative delta |
| Writes to protective, interlock and alarm-limit registers | tier write allowlist, running-write check |
| An agent contradicting what it just read | contradiction question |
| Prompt injection through device strings, tag names, node descriptions | the model never reads a string; typed state, closed vocabulary (`INJECTION.md`) |
| A compromised or careless client | tier demotion with hysteresis, block on repeated denies |
| Disputes about who changed what | hash-chained ledger with the prior value |

## What it does not do

- It is not a safety instrumented system and does not replace one. Hardwired E-stops, safety PLCs, interlocks and relief devices stay exactly where they are.
- It does not authenticate clients. Pair it with network segmentation and, on OPC UA, certificates.
- It does not inspect payload semantics beyond the register model. A legal write to a legal register at a legal value is allowed, even if the intent behind it is bad.
- It does not protect against a controller reached by a path it is not on, a physical access, or firmware-level attacks.
- Fail behaviour of the host is the host's. If the proxy process dies, what happens next depends on where you run it. Fail-closed on a switch, fail-open on a laptop. Decide on purpose.

## Operating advice

1. Start in shadow mode. Read the ledger for a few days. Promote to enforcing when the would-have-denied rows are the ones you want.
2. Keep stop-bypass on unless you have a specific reason. Stops should never wait for a model.
3. Register your agents. Unregistered clients start with the smallest budget and never reach trusted.
4. Put three or four traps per plant inside real address ranges.
5. Export the ledger. It is append-only JSONL with a sha256 chain; `Ledger.verify()` checks it.

## Reporting a weakness

See `SECURITY.md`. Please do not open public issues for exploitable findings.
