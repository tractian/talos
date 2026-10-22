# Security policy

Talos sits in the request path to industrial controllers. We treat weaknesses in it seriously and want to hear about them privately first.

## Reporting

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). If that is not available to you, email security@tractian.com with "Talos" in the subject.

Please include: the version or commit, the configuration (proxy flags, map or blind mode, model or `--model null`), a reproduction, and what you believe the impact is.

What we commit to:

- acknowledge within 3 business days
- a first assessment within 10 business days
- a fix or mitigation plan, and a coordinated disclosure date, agreed with you
- credit in the release notes if you want it

## Scope

In scope:

- the gate (`talos/`): any way to get a request past a rule or the policy that should have stopped it, including identity confusion, budget bypass, trap evasion by a non-enumerating client, cache poisoning, ledger chain forgery
- the Modbus proxy: malformed frames, parsing errors, resource exhaustion, anything that makes it fail open when it should fail closed
- the model as deployed: inputs crafted through legal protocol fields that move the model's output (string-bearing fields must never reach it; if you find one that does, that is a finding)
- `talos-pull` and weight loading: path traversal, unverified downloads

Out of scope:

- a legal request to a legal register at a legal value that the model allows. The gate judges behaviour; it does not know intent.
- the simulator and training pipeline (not in this repository)
- the base model and third-party dependencies (report upstream; tell us too)
- denial of service against the proxy by a client that is already blocked at the network level

## Safe harbour

Good-faith research that respects this policy, avoids privacy violations and service disruption, and does not touch systems you do not own or have permission to test, will not lead us to pursue legal action. Do not test against live industrial systems you are not authorised to test.

## Supported versions

The latest release on `main` and the latest tagged model. Older tags receive fixes only for critical findings.
