# Quarantine (the abstain band, made operational)

Network people already run this pattern. Cisco ISE Adaptive Network Control applies a quarantine policy to an endpoint on a security event and issues a Change of Authorization so the access device re-authorizes it into a restricted VLAN or SGT; release returns it to its normal access. UniFi does the coarse version with client isolation and per-client block. Talos does the same for agents, but graded, and driven by a calibrated score instead of a signature.

## Tiers

| tier | budget | writes | model band | exit |
|---|---|---|---|---|
| trusted | large | yes | allow unless p(deny) high | manual demotion or sustained bad reputation |
| normal | default | yes | full policy table | up after sustained clean history; down on reputation drop |
| quarantine | small | **read-only** (writes deferred and flagged; stops deniable if stop-bypass off) | strict thresholds | up after N clean requests over T minutes; down to blocked on trap touch or repeated denies |
| blocked | zero | no | n/a | manual release only |

## Entry rules

- model `misbehaving` above threshold for K of the last M requests (hysteresis, no single-request flips)
- trap touch -> quarantine immediately (blocked on second touch)
- flood: the fastpath defers an agent for budget 300 times inside 30 s (`flood_over_budget` / `flood_window_s`). This needs no
  model answer at all, so a flooder is quarantined even when the model is absent or timing out. An agent merely over its
  budget (a dashboard polling at 2x) is deferred, not quarantined: the count has to be sustained.
- fastpath hard-clamp violations repeatedly

## What quarantine changes

- budget shrinks; reads still served (from cache where possible) so dashboards degrade instead of dying
- writes are deferred with `retry_after_ms` and a ledger flag for a human
- the agent's requests are rendered with `tier=quarantine`, which the model is trained to treat as prior evidence

## Why not just deny

A deny is a decision about one request. Quarantine is a decision about a client. Separating them keeps the per-request false-deny rate low (the number that annoys people) while still containing a client that is clearly wrong.
