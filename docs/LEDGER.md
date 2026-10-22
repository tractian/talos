# The ledger

Every decision is one row in an append-only JSONL file with a sha256 hash chain. The file is the record a person can check: who asked, what for, what the gate did, why, and what the model thought.

## Row

```json
{
  "agent": "a:7f2c3a",
  "op": "write",
  "tag": "t:pump1.speed_ref",
  "cls": "setpoint",
  "value": 5800,
  "action": "deny",
  "returned": "deny",
  "by": "model",
  "tier": "normal",
  "reasons": ["envelope:outside", "deny_p:0.91"],
  "answers": {"action": "deny", "needed_now": 0.12, "duplicate": 0.0, "servable": 0.0, "over_budget": 0.05,
              "contradiction": 0.01, "state_conflict": 0.03, "envelope": "outside", "misbehaving": 0.21, "severity": 2.8},
  "state": "op=write proto=modbus cls=setpoint ... envelope=outside tier=normal rep=good ...",
  "latency_ms": 5.1,
  "shadow": false,
  "n": 18231,
  "t": 1792616251.51,
  "prev": "81844d19...",
  "hash": "fd7c4d91..."
}
```

| field | meaning |
|---|---|
| `agent` | interned agent id (see `IDENTITY.md`) |
| `op`, `tag`, `cls`, `value` | the request. `tag` is the map name or `addr:<n>` in blind mode. |
| `action` | what the gate decided |
| `returned` | what the client actually received. Differs from `action` only in shadow mode, where every request is returned as `allow`. |
| `by` | who decided: `traps`, `fastpath`, `cache`, `model`, `policy`, `fallback` (model failed, fastpath verdict used) |
| `tier` | the agent's tier after this request |
| `reasons` | short machine-readable reasons; in shadow mode the first one is `shadow:would=<action>` |
| `answers` | the model's calibrated answers to the fixed question set, empty when the model did not run |
| `state` | the exact typed state the model saw (closed vocabulary, no free text) |
| `latency_ms` | gate time for this request, including the model |
| `n`, `t` | row number and wall-clock time |
| `prev`, `hash` | chain: `hash = sha256(sorted-key JSON of the row without hash)`, `prev` is the previous row's hash |

## Verify

```python
from talos.ledger import Ledger
ok, bad_row = Ledger("talos-ledger.jsonl").verify()
```

`ok` is `False` and `bad_row` is the first row whose `prev` or `hash` does not match. A ledger that has been edited, truncated in the middle, or had rows reordered fails here.

## Ask it things

It is JSONL. Any tool works.

```bash
# who wrote to the conveyor speed, and what did the model think
jq -c 'select(.tag=="t:conv1.speed_ref" and .op=="write") | {t, agent, value, action, by, reasons}' talos-ledger.jsonl

# everything that would have been denied in shadow mode
jq -c 'select(.shadow and .action=="deny")' talos-ledger.jsonl

# agents by number of denies
jq -r 'select(.action=="deny") | .agent' talos-ledger.jsonl | sort | uniq -c | sort -rn
```

## Properties

- Append-only. The gate opens the file in append mode and continues the chain from the last row on restart.
- No free text. Tag names in the row are map names you chose; device strings and agent-supplied text never enter the ledger.
- `fsync` is off by default for throughput; pass `Ledger(path, fsync=True)` for durability per row.
- Rotation: close the gate, move the file, start again. The new file starts a new chain from the zero hash. Keep the old file; its chain stands on its own.

Code: `talos/ledger.py`.
