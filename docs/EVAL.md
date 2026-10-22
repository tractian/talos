# How Talos is measured

Numbers in the README come from a held-out test: one whole plant family and three agent personas that never appeared in training, rendered both with and without a register map. The evaluation suite itself is not published; the method is.

## What counts

For a gate, four-way action accuracy (allow / cache / defer / deny) is the wrong headline. Most disagreements between the model and the reference are a read served from cache instead of forwarded, which costs nothing. The numbers that matter are:

| metric | definition |
|---|---|
| bad requests caught | of requests the reference denies, the share the model also denies |
| good requests wrongly blocked | of requests the reference does not deny, the share the model denies |
| missed | of requests the reference denies, the share the model lets through (allow, cache or defer) |
| when confident | accuracy on requests where the model's top action has p >= 0.9, and the share of requests that covers |
| blind vs mapped | the same, split by whether the request was rendered with a map or from fingerprints alone |
| catch-within-N | requests until a flooder, prober or drifter is moved to quarantine |
| stops denied for non-quarantined agents | must be zero |
| string fuzz | movement of the model's output when every string-bearing field is filled with injection payloads: must be zero |
| latency | p50 / p99 of the full gate path on a laptop CPU |

Per-question AUROC, Brier and ECE are tracked too, and reported per held-out family and persona with the worst cell shown.

## Reference labels

Labels come from a programmatic oracle that sees the full ground truth of the simulated plant (every register's true limits, state and history) and applies fixed rules. No model and no human labelled the training or test data. The model learns to reproduce the oracle's verdict from only what a gateway can observe.

## Baselines

Every release is compared against a gradient-boosted tree on the same typed features and against the deterministic rules alone. The tree is the floor the model has to beat on false blocks; the rules are what you get with `--model null`.

## Reproducing on your traffic

Run the proxy in shadow mode on a real network for a few days. The ledger then contains every request, the decision, the typed state and the model's answers. Count the would-have-denied rows and read them. That is the evaluation that matters for your plant, and it needs nothing from us.
