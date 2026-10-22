# Try Talos in five minutes

A fake PLC, the Talos proxy in front of it, and a few scripted agents that behave well or badly. Everything runs on
localhost; the proxy decides what reaches the PLC and keeps a hash-chained ledger of every decision.

```
pip install "talos-gate[onnx,modbus]"
talos-pull                      # optional: student weights into ./weights (without them the proxy runs the fast path only)
```

Four terminals (or background the first two):

```
# 1. the plant: a fake Modbus PLC on :15020 described by examples/world.json
python examples/fake_plc.py

# 2. Talos in front of it, listening on :15502; the four agents below are registered by their Modbus unit id
talos-proxy --upstream 127.0.0.1:15020 --world examples/world.json --weights weights/ --report-every 200 \
    --agent dashboard=127.0.0.1:1:hmi --agent prober=127.0.0.1:2:copilot --agent flood=127.0.0.1:3:copilot --agent drifter=127.0.0.1:4:optimizer

# 3. agents, one at a time, pointed at the proxy
python examples/bad_agent.py --target 127.0.0.1:15502 --script dashboard    --seconds 10   # polite polling: allowed
python examples/bad_agent.py --target 127.0.0.1:15502 --script decoy_prober --seconds 20   # trap registers: denied (0x04)
python examples/bad_agent.py --target 127.0.0.1:15502 --script flood        --seconds 10   # over budget: deferred (0x06) for as long as it hammers
python examples/bad_agent.py --target 127.0.0.1:15502 --script drifter      --seconds 30   # setpoint walked toward the envelope edge: the model denies the drift

# 4. what the gate did: the proxy's report line (stderr) and the ledger
python -c "from talos.ledger import Ledger; print(Ledger('talos-ledger.jsonl').verify())"   # (True, None) = chain intact
```

Leave the `--agent` lines out and the same scripts run as unknown agents: the gate gives an unregistered source a
quarantine-sized budget (20 reads and a few writes a minute), so even the dashboard sees `0x06` after a burst.

`talos-proxy` without `--weights` looks in `$TALOS_WEIGHTS`, then `./weights`; with neither it says so and runs the
deterministic fast path alone (`--model null` asks for that explicitly). Traps, budgets and the connection-close on a
blocked agent work without the model; the drifter is the model's job (the fast path only feeds it the drift
features), so with `--model null` its writes go through, and with the weights they stop before the envelope edge.
`fake_plc.py` shows which writes landed either way.

Each ledger row is one decision: who asked, what, the action and why (`by`), chained by sha256 so a row cannot be
edited or removed without `verify()` noticing. `--ledger PATH` moves it; the default is `./talos-ledger.jsonl`.

`fake_plc.py` prints every write that reaches it: run the drifter against the PLC directly
(`--target 127.0.0.1:15020`) to see the same writes land unopposed.
