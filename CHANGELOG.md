# Changelog

All notable changes to the code are recorded here. Model releases are tracked on the Hugging Face model page and tagged here as `model-<name>`.

## Unreleased

### Fixed (first run on real hardware, an Eaton PowerXL DM1 behind a Jetson, 2026-10-22)
- A model timeout or crash is a model failure, never a dropped client: on Python 3.10 the asyncio timeout escaped the gate and the proxy closed the operator's connection with a stop still in flight. Both paths now fall back to the rules and the proxy answers the frame; only an upstream (controller) socket error closes the client. A frame refused because of a gate bug is still written to the ledger.
- One judge inference in flight at a time: a judge slower than its deadline no longer piles up threads until every request times out on queue wait; the second caller falls back at once.
- Bookkeeping after a forwarded write can no longer report a write the controller already executed as a failure.
- Budget is charged per wire frame, not per register: an HMI block poll or block write is one token (reads wider than 16 registers pay one per 16, so sweeps still hit the budget).
- `stop_value` on command registers (raw register units, hex accepted): bit-coded control words (Eaton `0x0300`) are stop-class and bypass the model like a speed-to-zero write. `talos.map/1` files from `talos-importer` load directly and their `bits.stop_word` is honoured.
- Python 3.10 is supported and in CI: it is what JetPack / Ubuntu 22.04 edge boxes run, and it is where the dropped-client bug lived.

### Added
- Talos gate: identity and tiers, trap registers, deterministic fastpath (allowlist, clamps, budgets, cumulative delta, floods), value and decision caches, typed closed-vocabulary rendering, policy table with quarantine hysteresis, sha256-chained ledger.
- Modbus TCP proxy (`talos-proxy`), standard library only.
- ONNX student judge (`talos.model.onnx`) and System One HTTP judge (`talos.model.systemone`).
- Blind (map-free) mode with register fingerprints learned from traffic and a cold-start rule.
- Tag-export importer (`talos-importer`) to build a map from a CSV/JSON tag list.
- `talos-pull` to fetch weights from Hugging Face.
- Examples: fake PLC, misbehaving agent scripts.

### Model
- `talos-0.8b` and its 5 MB int8 student: first public weights.
