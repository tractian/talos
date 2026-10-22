# Quickstart

Five minutes, one laptop, no PLC. A fake controller, Talos in front of it, and a misbehaving client.

```bash
pip install "talos-gate[onnx,modbus] @ git+https://github.com/tractian/talos"   # the import is talos
talos-pull                      # downloads the weights into ./weights
```

Terminal 1, a fake PLC on port 15020 with a small map and a couple of trap registers:

```bash
python examples/fake_plc.py
```

Terminal 2, Talos between the clients and the PLC. The `--agent` flags register the four demo clients by unit id so each gets a normal budget; an unregistered source gets the small unknown-client budget and is deferred after its first burst:

```bash
talos-proxy --upstream 127.0.0.1:15020 --listen 15502 --world examples/world.json --weights weights/ --report-every 200 \
  --agent dashboard=127.0.0.1:1:hmi --agent prober=127.0.0.1:2:copilot --agent flood=127.0.0.1:3:copilot --agent drifter=127.0.0.1:4:optimizer
```

Terminal 3, clients. Run them one at a time or all at once; they talk to 15502, never to the PLC.

```bash
python examples/bad_agent.py --script dashboard      # polite polling: allowed, mostly served from cache
python examples/bad_agent.py --script flood          # polls everything as fast as it can: deferred, then quarantined
python examples/bad_agent.py --script decoy_prober   # touches a trap register: denied, then blocked (connection closed)
python examples/bad_agent.py --script drifter        # walks a setpoint out of its envelope in small steps: the model catches the drift
```

Watch terminal 2. Then check the record:

```bash
python -c "from talos.ledger import Ledger; print(Ledger('talos-ledger.jsonl').verify())"
jq -c '{agent, op, tag, action, by, tier, reasons}' talos-ledger.jsonl | tail -20
```

## Against your own controller

```bash
talos-proxy --upstream <plc-ip>:502 --listen 15502 --shadow --weights weights/
```

Point one client at the proxy. Shadow mode judges and logs but never blocks. Read the ledger for a day. Drop `--shadow` when the rows it would have denied are the ones you want gone. No map is needed; add one later for tighter clamps (`WORLD-FORMAT.md`).

## Without the model

```bash
talos-proxy --upstream 127.0.0.1:15020 --model null
```

Rules only: budgets, traps, clamps, floods, ledger. Everything the model adds is on top of this.
