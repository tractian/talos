# Trap registers

A trap is an address no legitimate client has any reason to touch. It is declared in the map (`"cls": "trap"` or `"is_trap": true`) and, in the Modbus proxy, it is just a holding register that happens to exist. Nothing announces it.

What a touch does:

1. The request is denied before the model runs (`by: "traps"` in the ledger).
2. The agent takes a reputation hit and moves to `quarantine` immediately.
3. A second touch, or any touch while already quarantined, moves the agent to `blocked`. The proxy closes the connection.

Why it works: good agents only touch addresses that a map, a tag list, or an operator gave them. The clients that touch a trap are the ones that enumerate address space (a scanner, a scraper reading every register, an LLM agent guessing addresses, a prober). That is exactly the population you want ranked out first, and it costs no model call.

Placement advice:

- Put traps inside the real address ranges, not at the end. Scanners that stop at the first exception never reach a trap parked at 9999.
- One trap per asset block is enough. Three or four per plant catches every enumeration pattern we have seen.
- Do not reuse an address a vendor tool is known to poll for identification.
- Traps work in blind mode too: pass a minimal world that only declares traps, and everything else is learned from traffic.

Trap touches are never counted against the false-deny rate. A trap deny is correct by definition.

Code: `talos/traps.py` (`TrapSet`, `from_world`, `is_trap`).
