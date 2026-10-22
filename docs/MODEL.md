# The model

Talos is a System One decision model: it answers a fixed set of typed questions about one request in a single forward pass and returns calibrated probabilities. It does not generate text. It cannot be prompted.

## Input

A typed state (`SCHEMA.md`) rendered in a closed vocabulary of a few hundred symbols: the request (op, register class, count), what the gateway knows about the register (limits, envelope, fingerprint from traffic), the write arithmetic (delta, rate, drift), the agent (tier, reputation, budgets, recent history) and the plant state. No tag names, no device strings, no free text. See `INJECTION.md`.

## Output

Ten questions, all at once:

`action` (allow / cache / defer / deny), `needed_now`, `duplicate`, `servable`, `over_budget`, `contradiction`, `state_conflict`, `envelope` (inside typical / atypical / at limit / outside), `misbehaving`, `severity` (0 to 4).

Each answer is a calibrated probability or score. The policy table turns them into the final action and the tier update.

## Two sizes

| | Talos-0.8B | student |
|---|---|---|
| what | LoRA fine-tune of a 0.8B base with a pointer readout, built on the Kev recipe | 6-layer, 256-wide encoder over the same symbol vocabulary, distilled from the 0.8B |
| size | ~1.6 GB bf16 | 5 MB int8 ONNX |
| latency | tens of ms on a GPU, served over HTTP | ~5 ms on a laptop CPU, in-process |
| where | a server next to the gateway | inside the proxy, on a switch, an industrial PC, a Raspberry Pi |

The proxy ships with the student. The 0.8B is for a gateway with a GPU, or for distilling your own student after fine-tuning.

## Training data

Fully synthetic. Simulated plants with physics, controllers exposed over Modbus and OPC UA, and a swarm of scripted and LLM-driven agents with good, sloppy and bad intents. Labels come from a programmatic oracle with full ground truth. No real plant traffic, tag names, or customer data were used, and none appear in the weights.

## Calibration and abstaining

Each question head has a fitted temperature. When no action reaches the abstain threshold the model abstains and the deterministic rules decide. In practice the model is confident on about four in five requests and is right on about 99 of 100 of those.

## Limits

- Trained on simulated plants. A real plant with register semantics far from anything in the simulator will see more false blocks until the fingerprints form. Shadow mode first.
- Blind mode (no map) matches mapped mode on accuracy but blocks more good writes early in an episode, before the register's history exists. The cold-start rule defers those instead of denying.
- The model only sees what the gateway sees. A plant state the gateway cannot infer is rendered as unknown.
- The model can only make a decision stricter than the rules. It never allows what a rule denied.

## Files

```
weights/
  student.int8.onnx   the model
  vocab.json          the symbol vocabulary and packing order
  heads.json          per-question head layout and temperatures
```

`talos-pull` fetches them from `tractian-ai/talos-student`. The 0.8B adapter lives beside it at `tractian-ai/talos-0.8b`.

## Lineage

Built with Kev (Apache-2.0), an open reproduction of the System One idea, on a Qwen base model. See `NOTICE` for attributions.
