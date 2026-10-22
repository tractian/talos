<p align="center"><img src="docs/assets/talos-wordmark.svg" alt="Talos" width="360"></p>

<p align="center">
<a href="https://github.com/tractian/talos/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/tractian/talos/ci.yml?style=for-the-badge&labelColor=000000" height="28"></a>
<a href="https://huggingface.co/tractian-ai/talos-0.8b"><img alt="Weights: Talos-0.8B and student int8" src="https://img.shields.io/badge/WEIGHTS-0.8B%20%C2%B7%20student%20int8-0a0a0a.svg?style=for-the-badge&labelColor=000000" height="28"></a>
<a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-0a0a0a.svg?style=for-the-badge&labelColor=000000" height="28"></a>
</p>

**Talos is an open System One decision model that stands between AI agents and the controllers that run the physical world.** One agent or a thousand: it keeps them well behaved on OT, keeps them out of each other's way, and logs who did what.

It starts blocking bad agents right away, and it gets stricter about bad values as it learns the plant.

## What it does

Every Modbus or OPC UA request from any client goes through one gate. The gate answers a fixed set of typed questions about the request in a single forward pass of a small model, about 5 ms on a laptop CPU, and does one of four things:

| decision | meaning |
|---|---|
| **allow** | forward to the controller |
| **cache** | answer from the gate's own copy; the controller is not touched |
| **defer** | not now; retry after the agent's budget refills |
| **deny** | never, and the agent's reputation takes the hit |

Around the model sit things that need no model at all: per-agent budgets by tier, trap registers that catch anything enumerating address space, hard clamps from the map, a flood rule, and a sha256-chained ledger of every decision with the exact state the model saw.

Humans go through the same gate. An operator's screen keeps its read budget and its stop commands always pass, but a fat-fingered setpoint gets the same look an agent's would. There is no special lane to hijack.

## Numbers

Tested on a plant type and three agent types the model never saw in training (15,593 requests):

| | |
|---|---|
| bad requests caught | **98%** (1,043 of 1,062) |
| good requests wrongly blocked | **0.6%** (84 of 14,531) |
| right when confident (80% of requests) | **99.0%** |
| register map required | **no.** Drop it on the wire blind: same 98% catch rate, under 1% false blocks (0.2% with a map) |

Twenty agents hammering one pump station for 60 s, with and without Talos in the path:

| | without | with Talos |
|---|---|---|
| requests reaching the PLC per second | 1,831 | 79 |
| bad requests that reached the PLC | 111,146 | 29 |
| illegal addresses forwarded | 0 | 0 |
| operator screen read p99 | 1.4 ms | 8.6 ms |

How these are measured: [docs/EVAL.md](docs/EVAL.md).

## Five minutes

```bash
pip install "talos-gate[onnx,modbus] @ git+https://github.com/tractian/talos"   # import talos
talos-pull                                   # 5 MB of weights into ./weights
python examples/fake_plc.py                  # terminal 1: a fake PLC on :15020
talos-proxy --upstream 127.0.0.1:15020 --listen 15502 --world examples/world.json --weights weights \
  --agent dashboard=127.0.0.1:1:hmi --agent prober=127.0.0.1:2:copilot --agent flood=127.0.0.1:3:copilot --agent drifter=127.0.0.1:4:optimizer   # terminal 2
python examples/bad_agent.py --script flood  # terminal 3: watch it get deferred, then quarantined
```

Against your own controller, start in shadow mode and read the ledger before you let it block anything:

```bash
talos-proxy --upstream <plc-ip>:502 --listen 15502 --shadow --weights weights
```

No map needed. Add one later for tighter clamps. Full walkthrough: [docs/QUICKSTART.md](docs/QUICKSTART.md).

## How it works

```
client ──▶ identity ──▶ traps ──▶ fastpath rules ──▶ cache ──▶ render ──▶ model ──▶ policy ──▶ ledger ──▶ controller
                                                               (typed state, closed vocabulary, no strings)
```

Each stage can only make the outcome stricter than the one before. The model never loosens a rule.

- **The model never reads a string.** Tag names, device identification, node descriptions and anything an agent says are interned to ids before rendering. There is no field an injected sentence can ride in. [docs/INJECTION.md](docs/INJECTION.md)
- **Agents earn a tier.** Trusted, normal, quarantine, blocked. Floods, retry loops, scope creep and trap touches move an agent down with hysteresis; a clean history moves it up. [docs/QUARANTINE.md](docs/QUARANTINE.md)
- **Blind mode.** With no map, every register gets a fingerprint from traffic: who writes it, how it moves, its observed range. The model reasons from that. [docs/PIPELINE.md](docs/PIPELINE.md)
- **The ledger.** Append-only JSONL, hash-chained, with the prior value and the model's answers on every row. `jq` is the query language. [docs/LEDGER.md](docs/LEDGER.md)

Docs: [schema](docs/SCHEMA.md) · [config reference](docs/CONFIG.md) · [the map format](docs/WORLD-FORMAT.md) · [trap registers](docs/TRAPS.md) · [identity](docs/IDENTITY.md) · [the proxy](docs/PROXY.md) · [the model](docs/MODEL.md) · [threat model](docs/THREAT-MODEL.md)

## The model

Two sizes, same questions, same vocabulary:

- **Talos-0.8B**: a LoRA fine-tune of a 0.8B base with a pointer readout, built on the [Kev](https://github.com/jaredpalmer/kev) recipe. For a gateway with a GPU.
- **the student**: 5 MB int8 ONNX, distilled from the 0.8B, ~5 ms on a CPU. What the proxy ships with. Small enough for a switch.

Training data is fully synthetic: simulated plants, controllers over Modbus and OPC UA, and a swarm of good, sloppy and bad agents, labelled by an oracle with full ground truth. No real plant traffic or names anywhere. [docs/MODEL.md](docs/MODEL.md)

## Status

v0.1. Modbus TCP proxy and the Python gate are usable today. OPC UA and MCP front ends are the next thing; the gate is protocol-neutral and the schema is the contract, so if you build one we want it. See [CONTRIBUTING.md](CONTRIBUTING.md).

Read [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) before putting it in front of anything that moves.

## License

Code and weights: [Apache 2.0](LICENSE). Built with Kev (Apache 2.0) on a Qwen base model; see [NOTICE](NOTICE).

Talos is a TRACTIAN project.
