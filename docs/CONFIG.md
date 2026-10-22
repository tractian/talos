# Configuration reference

Everything is a Python dataclass with defaults. The proxy exposes the common ones as flags; a library user builds the objects directly.

```python
from talos.gate import Gate, GateConfig
from talos.fastpath import FastpathConfig
from talos.policy import PolicyConfig

cfg = GateConfig(model_deadline_ms=20, fastpath=FastpathConfig(), policy=PolicyConfig())
gate = Gate(cfg, world=None)        # world=None is blind mode
```

## GateConfig (`talos/gate.py`)

| field | default | meaning |
|---|---|---|
| `stop_bypass` | `True` | A write in the register's safe direction (a stop, a slow-down, a close) skips the model and is allowed. Off: it still goes through the model, but `deny` is only possible for `quarantine` and `blocked` agents. |
| `model_deadline_ms` | `20.0` | Hard deadline for the model answer. On timeout the fastpath verdict stands. |
| `on_model_failure` | `"defer"` | What a request with no fastpath verdict gets when the model fails: `defer` (safe) or `fastpath_only` (allow and flag). |
| `decision_cache_ttl_s` | `2.0` | An identical request from the same agent inside this window replays the last decision. |
| `value_ttl_s` | `2.0` | Default freshness for serving reads from cache; a tag's own `ttl_s` wins. |
| `trusted_reads_skip_model` | `False` | Opt-in fast lane: trusted-tier reads bypass the model. Off by default so humans and agents go through the same gate. |
| `map_mode` | `"auto"` | How the state is rendered for the model: `auto` (mapped tags as `declared_fp`, unmapped as `fingerprint`), or force `declared`, `declared_fp`, `fingerprint`. |
| `shadow` | `False` | Judge and log, never block. Every decision is recorded with what Talos would have done. Use this for the first days on a real network. |
| `fastpath` | `FastpathConfig()` | See below. |
| `policy` | `PolicyConfig()` | See below. |
| `budgets` | `DEFAULT_BUDGETS` | Per-tier request budgets, see Tiers. |

## FastpathConfig (`talos/fastpath.py`)

Deterministic rules that run before the model. They can only make a decision stricter; the model never loosens them.

| field | default | meaning |
|---|---|---|
| `stop_bypass` | `True` | Same switch as above, seen by the rules. |
| `write_classes` | `DEFAULT_WRITE_CLASSES` | Which register classes each tier may write (`setpoint`, `command`, `tuning`, ...). A write outside the tier's classes is denied. |
| `allow_unknown_reads` | `True` | Reads of unmapped addresses pass to the model instead of being denied outright. |
| `coldstart` | `True` | Blind mode only: a write to a register nobody has ever written is deferred and flagged, never denied by this rule. |
| `cum_delta_window_s` | `600` | Window for the cumulative-change accumulator per register. |
| `cum_delta_cap_frac` | `0.6` | Deny when one agent's accumulated change on a register exceeds this fraction of its range inside the window. |
| `drift_min_steps` | `5` | Steps in one direction before a slow ramp counts as drift (feeds the model). |
| `shock_delta_frac` | `0.5` | A single write moving a register by more than this fraction of its range is a shock (feeds the model). |
| `running_write_check` | `True` | Deny writes to registers marked `writable_while_running: false` while the asset runs. |
| `duplicate_ack` | `True` | A write that sets the value the register already holds is acknowledged from cache, never forwarded. |

## PolicyConfig (`talos/policy.py`)

Thresholds over the model's calibrated answers, plus the tier state machine.

| field | default | meaning |
|---|---|---|
| `deny_p` | `0.80` | p(deny) above this denies. |
| `deny_severity` | `3.0` | Expected severity at or above this denies (3 = harmful). |
| `deny_state_conflict` | `0.90` | p(state_conflict) above this denies. |
| `defer_p` | `0.50` | p(defer) above this defers. |
| `defer_over_budget` | `0.80` | p(over_budget) above this defers. |
| `defer_misbehaving` | `0.70` | p(misbehaving) above this defers a write. |
| `defer_contradiction` | `0.85` | p(contradiction) above this defers. |
| `cache_servable` | `0.80` | p(servable) above this serves a read from cache. |
| `cache_duplicate` | `0.90` | p(duplicate) above this acks a write from cache. |
| `abstain_max_p` | `0.45` | If no action reaches this probability the model abstains and the fastpath verdict stands. |
| `trusted_deny_p` | `0.95` | Trusted tier needs this much to be denied. |
| `misbehaving_hit` | `0.70` | p(misbehaving) above this counts as a strike. |
| `quarantine_k` / `quarantine_m` | `3` / `5` | K strikes in the last M requests moves an agent to quarantine. |
| `quarantine_exit_clean` | `20` | Clean requests needed to leave quarantine... |
| `quarantine_exit_min_s` | `120` | ...and the minimum time spent there. |
| `block_on_denies` | `8` | Denies inside `deny_window_s` moves an agent to blocked. |
| `deny_window_s` | `300` | |
| `trusted_after_clean` | `500` | Clean requests before a registered agent is promoted to trusted. |
| `defer_retry_ms` | `500` | `retry_after` sent with a defer. |
| `quarantine_retry_ms` | `5000` | `retry_after` sent to a quarantined agent. |
| `flood_window_s` / `flood_over_budget` | `30` / `300` | An agent deferred for budget this many times inside the window is quarantined without any model answer. |

## Tiers and budgets (`talos/identity.py`)

Requests per minute by tier. Reads cover read, subscribe and browse; writes cover write and call.

| tier | reads/min | writes/min | notes |
|---|---|---|---|
| trusted | 600 | 120 | registered agents and operator screens after a clean history |
| normal | 120 | 30 | every registered agent starts here |
| quarantine | 20 | 0 | reads served, mostly from cache; writes deferred and flagged |
| blocked | 0 | 0 | connection closed; manual release only |

Unregistered sources get a synthetic id (`u:<hash>`) and sit in `normal` with a reduced budget (20 reads/min, 3 writes/min) until an operator registers them. Only registered agents can be promoted to `trusted`. See `IDENTITY.md` and `QUARANTINE.md`.

## Environment variables

| variable | used by |
|---|---|
| `TALOS_WEIGHTS` | default weights directory for the proxy and `talos-pull` |
| `TALOS_HF_REPO` | Hugging Face repo id for `talos-pull` (default `tractian-ai/talos-student`) |
| `TALOS_JUDGE_API_KEY` (or `OPENROUTER_API_KEY`) | bearer token for a remote System One judge |
| `TALOS_LLM_BASE`, `TALOS_LLM_MODEL` | OpenAI-compatible endpoint for the optional map importer's LLM classifier |
