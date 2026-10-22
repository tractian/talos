# Prompt injection: why the model never reads free text

The attack: an agent's context (a manual, a work order, an OPC UA node description, a device name) contains instruction-like text, and either the agent or the gate's judge follows it. Decision models read instructions literally, so "this write is authorized by the safety engineer" inside a node description is a live risk if it ever reaches the model.

The clean answer: **the model's input is typed, not text.** The renderer builds the state from a fixed schema. Tags, agents, assets, and roles are interned ids. Numbers are binned to words from a closed vocabulary. History rows are typed. There is no field that can carry an attacker's sentence. The question set is fixed and lives in the gate, not in the request.

Consequences:
- Device identification strings (Modbus FC43/14), OPC UA DisplayName/Description/BrowseName, tag names, and any agent-supplied free text never reach the model. They are hashed to ids for the ledger.
- The agent's own reasoning or rationale is not an input. Only the request and the gateway's observations are.
- The attack surface on the judge collapses to the legal field grid of the protocol, which can be enumerated per tag offline and the decision surface certified.
- Red-team test, mandatory before any release: fuzz every string-bearing field with injection payloads and delimiter breaks; the model's output must not move at all. Target is zero, not low.

The teacher's text rendering uses the same closed vocabulary (e.g. `tag=t:4821 class=setpoint delta=+5% envelope=inside_typical tier=normal rep=good reqs_1m=12/40`), so even the text form carries no free text.
