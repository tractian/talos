# Changelog

All notable changes to the code are recorded here. Model releases are tracked on the Hugging Face model page and tagged here as `model-<name>`.

## Unreleased

### Fixed (first run on real hardware, an Eaton PowerXL DM1 behind a Jetson, 2026-10-22)
- A model timeout or crash is a model failure, never a dropped client: on Python 3.10 the asyncio timeout escaped the gate and the proxy closed the operator's connection with a stop still in flight. Both paths now fall back to the rules and the proxy answers the frame.
- Read budget is charged per request, not per register, so an HMI block poll is one token.
- `stop_value` on command registers: bit-coded control words (Eaton `0x0300`) are stop-class and bypass the model like a speed-to-zero write.

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
