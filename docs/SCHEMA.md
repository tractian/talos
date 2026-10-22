# Schema

One request, one typed state, one fixed question set. The schema is the product: any gateway that can emit a `Request` and a `Context` can use any Talos model.

## Request (what the agent asked)

```json
{
  "agent_id": "a:7f3c",            // from identity; interned
  "protocol": "modbus" | "opcua",
  "op": "read" | "write" | "subscribe" | "browse" | "call",
  "target": {"tag": "t:pump2.speed_ref", "class": "setpoint", "writable_while_running": true, "safe_direction": "down"},
  "value": 4200,                   // raw, for writes
  "count": 1,                      // registers / nodes touched
  "ts_rel": 0                      // relative timing only; absolute never leaves the gateway
}
```

Register classes: `setpoint`, `command`, `tuning`, `protective`, `interlock`, `alarm_limit`, `config`, `diagnostic`, `trap`.

## Context (what the gateway knows at that instant)

```json
{
  "asset": {"kind": "vfd_pump", "state": "running" | "stopped" | "faulted" | "lockout" | "maintenance"},
  "target_now": {"value": 4000, "age_s": 0.4, "limits": [0, 6000], "envelope": [3500, 4800]},
  "write_features": {"delta_pct": 5.0, "limit_frac": 0.70, "envelope_pos": "inside_typical", "implied_rate": "normal", "cum_delta_bin": "low", "drift": "none"},
  "agent": {"tier": "normal", "rep_bin": "good", "reqs_1m": 12, "reqs_budget_frac": 0.3, "writes_1m": 1, "distinct_tags_1m": 4, "repeat_bin": "none", "last_read_same_tag_age_s": 2.1, "contradicts_recent_read": false},
  "plant": {"mode": "production" | "changeover" | "maintenance" | "startup" | "shutdown", "permit_open": false, "alarm_active": false},
  "history": [ {"op": "read", "tag": "t:pump2.speed_fb", "age_s": 2.1}, {"op": "write", "tag": "t:pump2.speed_ref", "delta_pct": 2.0, "age_s": 31} ],
  "cache": {"servable": true, "age_s": 0.4, "ttl_s": 2.0}
}
```

All numbers that matter are also binned into words by the renderer. Strings never appear: tags, agents, and assets are interned ids.

## Questions (fixed set, v0.1)

| id | type | options / statement |
|---|---|---|
| `action` | choice | `allow`, `cache`, `defer`, `deny` |
| `needed_now` | noul | The controller must be touched now to satisfy this request |
| `duplicate` | noul | The write sets a value the target already holds |
| `servable` | noul | A read can be answered from the cached value |
| `over_budget` | noul | The agent is exceeding a reasonable request rate for its role |
| `contradiction` | noul | The write contradicts what the agent read moments ago |
| `state_conflict` | noul | The machine state does not permit this request |
| `envelope` | choice | `inside_typical`, `atypical`, `at_limit`, `outside` |
| `misbehaving` | noul | The agent's recent behaviour is that of a faulty or hostile client |
| `severity` | score | 0 none, 1 wasteful, 2 disruptive, 3 harmful, 4 dangerous |

The policy table maps calibrated answers + fastpath hits to the final `action`, tier update, and ledger row.

## Decision (what the gate returns)

```json
{"action": "defer", "by": "model" | "fastpath" | "cache" | "policy", "p": {"deny": 0.12, "defer": 0.61, "cache": 0.05, "allow": 0.22}, "tier": "normal", "reasons": ["over_budget:0.83"], "retry_after_ms": 500, "ledger_id": "..."}
```

## Labelled record (training format)

Kev's labelled-request format, so the teacher trains unchanged:

```json
{"state": <Context + Request as a compact typed text>, "questions": {"action": {"type": "choice", "instructions": "...", "criteria": {...}, "label": "defer", "src": "talos:plantfam=pump_station:persona=looper"}, ...}}
```

Student records use the same labels over the symbol-token rendering.
