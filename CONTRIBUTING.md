# Contributing

Thanks for looking. Talos is small on purpose: a typed schema, a handful of deterministic rules, a policy table, a ledger, and a model that answers a fixed question set. Changes that keep it that way are welcome.

## Setup

```bash
git clone https://github.com/canyon-tractian/talos-public.git talos && cd talos
make dev          # venv + editable install with dev, onnx, http, modbus extras
make test
make lint
```

Python 3.10 or newer: 3.10 is what JetPack / Ubuntu 22.04 edge boxes ship, so it stays in CI (onnxruntime 1.23 is the last build for it). Tests run in a few seconds and need no weights; the ONNX tests skip when `weights/` is absent (`make weights` fetches them).

## What helps most

- **Protocol front ends.** The gate is protocol-neutral; the proxy is Modbus TCP. An OPC UA gateway, an MCP server, or an HTTP front end that produces a `Request` and a `Context` (see `docs/SCHEMA.md`) is the most useful kind of contribution.
- **Fastpath rules** that are deterministic, explainable, and only ever make a decision stricter.
- **Fixtures.** Hand-written maps and traffic patterns from real plant types (with made-up names) make the tests better.
- **Bug reports with a ledger.** A ledger excerpt (it contains no free text) plus the proxy flags is usually enough to reproduce.

## Ground rules

- The model never reads a string. Any change that routes free text toward the renderer will be declined.
- Rules can tighten a decision; nothing in the gate may loosen one the fastpath made.
- Stops and safe-direction writes never wait for the model unless the operator turned stop-bypass off.
- No real plant data, tag names, customer names or network addresses in tests, fixtures or docs.
- Keep dependencies at the floor: the core imports with pydantic, numpy, orjson and pyyaml only.

## Pull requests

- One change per PR, with a test.
- `ruff check .` and `pytest -q` pass.
- Note in the PR if the change alters any default (config field, threshold, budget). Defaults are documented in `docs/CONFIG.md`; update it in the same PR.
- By contributing you agree your contribution is licensed under Apache 2.0, the same as the project. No CLA.

## Model changes

Weights are trained outside this repository and published on Hugging Face. Issues about model behaviour are welcome; include the ledger row (the `state` field is the exact input the model saw).
