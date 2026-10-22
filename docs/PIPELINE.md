# Gate pipeline

Order is fixed. Each stage can only make the outcome stricter than the previous one.

1. **identity**: attribute the request to an agent id and tier. Unknown -> lowest tier.
2. **traps**: target is a trap register/node -> deny, reputation hit, ledger. No model.
3. **fastpath**:
   - unknown function code / service -> deny
   - tier allowlist (op, register class, address range, write permission) -> deny if outside
   - hard clamp (asset profile limits) -> deny if outside
   - stop-bypass: if enabled and the write is safe-direction -> allow, ledger, no model
   - rate budget (token bucket per agent, per class) -> defer if exhausted
   - cumulative-delta / drift accumulator per register -> flag (feeds the render; deny only above a hard cap)
4. **cache**: reads with fresh value -> cache; identical decision within TTL -> replay.
5. **render** -> **model** (deadline, e.g. 20 ms; on timeout use fastpath verdict).
6. **policy**: threshold table over calibrated probabilities + flags -> action; reputation update; quarantine transitions with hysteresis.
7. **ledger**: append row with hash chain.

Stop-bypass off: safe-direction writes go through 4-6 but `deny` is only permitted if the agent's tier is `quarantine` or `blocked`.

Fail modes: model unavailable -> fastpath-only mode and a ledger flag; gateway process crash -> the host's bypass behaviour (out of Talos's hands; documented per plugin).

## Blind (map-free) mode

A gate built with no world (`Gate(cfg, world=None)`, proxy started without `--world`) still runs the whole pipeline; what
changes is where register meaning comes from:

- **Tag names.** An address the map does not know is the tag `addr:<address>` (the same key the training records use, so
  fingerprints line up between training and the live gate). Coils and registers share that namespace.
- **Fingerprints.** `talos/fingerprints.py` keeps, per tag, what traffic alone shows: distinct writers, operator-side writes
  (by role from identity), writes in the last 10 min, moves-on-its-own, observed range, time since last write, seen by this
  agent / by anyone. Fed by every decision (who touched what) and by the data path (`observe_read` / `observe_write`, values on
  the wire). Semantics and binning are identical to the ones the model was trained on.
- **Rendering.** `GateConfig.map_mode`: `auto` (default) renders `declared_fp` for mapped tags and `fingerprint` for unmapped
  ones; a judge whose vocabulary predates fingerprints (`supports_fp` false) gets plain `declared`. Explicit values force one mode.
- **Write arithmetic.** With no limits, delta / rate / cumulative-delta / drift are measured against the observed range; the
  hard clamp never fires on an observed range (evidence for the model, not a rule).
- **Cold start.** Blind gate + write to a register nobody has ever written -> `defer` with reason `coldstart:unfamiliar_write`,
  never `deny` from this rule; the agent gets a misbehaving strike only when it repeats the write. Registers with write
  history go on to the model like any other. On a *mapped* plant an unmapped write stays `unknown_target` -> deny.
- **Allowlist.** Tier write classes apply only when the class is known; budgets, floods, traps, identity and the ledger
  need no map at all.
