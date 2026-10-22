---
language: en
license: apache-2.0
library_name: peft
base_model: Qwen/Qwen3.5-0.8B-Base
base_model_relation: adapter
pipeline_tag: text-classification
tags:
  - decision-model
  - system-one
  - calibration
  - lora
  - industrial-control
  - ot
  - modbus
  - opc-ua
  - qwen3.5
metrics:
  - accuracy
model-index:
  - name: Talos-0.8B
    results:
      - task:
          type: text-classification
          name: "allow / cache / defer / deny on agent requests, held-out plant type"
        dataset:
          type: synthetic
          name: "Talos run5 held-out plant family and personas (synthetic; not published)"
        metrics:
          - type: accuracy
            value: 0.936
            name: "4-way action accuracy"
---

# Talos-0.8B

A System One decision model for the plant floor. Given one request from a client (an AI agent, a script, an operator screen) to an industrial controller, plus what the gateway knows at that instant, it answers ten typed questions in a single forward pass and returns calibrated probabilities. It does not generate text and cannot be prompted.

Code, proxy, docs: https://github.com/tractian/talos

## Model description

- **Base:** Qwen/Qwen3.5-0.8B-Base (Apache 2.0)
- **Method:** LoRA (rank 16) on the frozen base plus a pointer readout over a fixed question set, trained with the [Kev](https://github.com/jaredpalmer/kev) recipe. One epoch over 161,602 labelled requests. Per-question temperature scaling on a held-out calibration partition.
- **Input:** a typed state rendered in a closed vocabulary. No tag names, device strings or free text ever reach the model.
- **Output:** `action` (allow / cache / defer / deny), `needed_now`, `duplicate`, `servable`, `over_budget`, `contradiction`, `state_conflict`, `envelope`, `misbehaving`, `severity`.

## Intended use

Inside the Talos gate, behind deterministic rules that it can only tighten. The gate serves reads from cache, defers over-budget clients, denies writes that should not land, and ranks clients into tiers by behaviour. Serve this model with `kev.serve` or any System One compatible server and point the gate at it with `--model systemone:URL`.

## Out of scope

- As a safety system. Talos is not a safety instrumented system and does not replace hardwired stops, safety PLCs, interlocks or relief devices.
- As an authentication or authorization mechanism. Identity on raw Modbus is a reputation key, not a credential.
- Judging anything that is not a typed request to a controller. There is no text interface.

## Training data

Fully synthetic. Simulated plants with physics (pumps, fans, conveyors, tanks, heaters, reactors, compressors and others), controllers exposed over Modbus and OPC UA, and a swarm of scripted and LLM-driven agents with good, sloppy and bad intents. Labels come from a programmatic oracle with full ground truth; no model or human labelled the data. No real plant traffic, tag names, or customer data were used. The dataset is not published.

## Evaluation

Held-out test: one plant family and three agent personas that never appeared in training, rendered both with a register map and from traffic fingerprints alone (15,593 requests).

| | |
|---|---|
| bad requests caught (reference deny, model deny) | 98.2% (1,043 of 1,062) |
| good requests wrongly blocked | 0.58% (84 of 14,531) |
| missed blocks | 0.12% (19) |
| 4-way action accuracy | 93.6% |
| accuracy when confident (p >= 0.9; 80% of requests) | 99.0% |
| with a register map: caught / wrongly blocked | 98.4% / 0.19% |
| with no register map: caught / wrongly blocked | 98.1% / 0.97% |
| misbehaving-agent AUROC | 0.992 |

Most 4-way disagreements are a read served from cache instead of forwarded, which has no cost. How these are measured: `docs/EVAL.md` in the repository.

## Limitations

- Trained on simulated plants. On a real plant whose register semantics are far from the simulator's, expect more false blocks until fingerprints form. Run the gate in shadow mode first.
- Without a map the model blocks more good writes early in an episode, before a register has any write history. The gate's cold-start rule defers those instead of denying.
- The model only sees what the gateway sees; plant state the gateway cannot infer is rendered as unknown.

## Files

- `adapter_model.safetensors`, `adapter_config.json`: the LoRA adapter (PEFT format)
- `head.pt`: the pointer readout and fitted temperature in Kev's native checkpoint format; this is what `kev.serve` loads
- `head.safetensors`: the same readout tensors for loaders that do not accept pickles
- `calibration.json`: the fitted temperature and head configuration, readable without torch
- `tokenizer.json`, `tokenizer_config.json`, `chat_template.jinja`: the base tokenizer
- `training_config.json`, `provenance.json`, `SHA256SUMS`

Serve: `uv run python -m kev.serve --run <this directory>` and point the gate at it with `talos-proxy --model systemone:http://127.0.0.1:8009`.

## Citation

See `CITATION.cff` in the repository.
