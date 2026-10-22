# The map (`world.json`)

Optional. Without one, Talos runs blind: every address is a fingerprint learned from traffic. With one, the model also sees the register's class, limits, envelope and safe direction, and the fastpath rules (clamps, write classes, running-write check) switch on.

```json
{
  "plant_id": "line-3",
  "assets": {"pump1": {"kind": "vfd_pump"}, "fan1": {"kind": "vfd_fan"}},
  "tags": {
    "t:pump1.speed_ref": {
      "asset": "pump1",
      "cls": "setpoint",
      "modbus": {"kind": "holding", "address": 100, "scale": 1},
      "limits": [0, 6000],
      "envelope": [3000, 5000],
      "writable_while_running": true,
      "safe_direction": "down",   // for a bit-coded control word use "stop_value": [768, 0] instead
      "ttl_s": 2.0
    },
    "t:pump1.max_freq": {"asset": "pump1", "cls": "protective", "modbus": {"kind": "holding", "address": 110}, "limits": [0, 6000], "writable_while_running": false},
    "t:pump1.speed_fb": {"asset": "pump1", "cls": "diagnostic", "modbus": {"kind": "input", "address": 200}, "ttl_s": 2.0},
    "t:pump1.trap_a":   {"asset": "pump1", "cls": "trap", "modbus": {"kind": "holding", "address": 150}}
  }
}
```

## Fields per tag

| field | required | meaning |
|---|---|---|
| `asset` | yes | Asset the register belongs to. Assets carry a `kind` and a running/stopped state the gateway infers. |
| `cls` | yes | One of `setpoint`, `command`, `tuning`, `protective`, `interlock`, `alarm_limit`, `config`, `diagnostic`, `trap`. Drives the tier write allowlist and how the model reads the request. |
| `modbus.kind` | yes (Modbus) | `holding`, `input`, `coil`, `discrete`. |
| `modbus.address` | yes (Modbus) | Zero-based register address. |
| `modbus.scale` | no | Raw-to-engineering multiplier. Default 1. |
| `opcua.node_id` | yes (OPC UA) | Node id string. |
| `limits` | no | Hard range `[lo, hi]`. A write outside is denied by the fastpath. |
| `envelope` | no | Typical operating range. The model sees where a write lands relative to it. |
| `writable_while_running` | no | Default `true`. `false` denies writes while the asset runs. |
| `safe_direction` | no | `down`, `up`, or `none`. A write toward safe is a stop-class write and passes under stop-bypass. |
| `stop_value` | no | One value or a list: the exact words that mean "stop" on a bit-coded command register (an Eaton fieldbus control word stops on `768`). Writes of these values are stop-class whatever `safe_direction` says. |
| `is_trap` | no | Same as `cls: trap`. |
| `ttl_s` | no | How long a cached read stays servable. Default 2 s. |

Partial maps are fine. Declare the registers you know; everything else is learned. A map that only lists traps is a reasonable day-one map.

Tag names never reach the model. They are interned to ids before rendering, so the map can use whatever names your plant uses.

A flat form is also accepted: a top-level object whose values are tag entries (anything with a `modbus` or `cls` key).

Code: `talos/world.py` (`World.load`, `World.from_dict`). Tag-export importer: `talos/importers/README.md`.
