---
language: en
license: apache-2.0
library_name: onnx
pipeline_tag: text-classification
tags:
  - decision-model
  - system-one
  - onnx
  - int8
  - edge
  - industrial-control
  - ot
  - modbus
  - opc-ua
  - knowledge-distillation
metrics:
  - accuracy
model-index:
  - name: Talos student (int8 ONNX)
    results:
      - task:
          type: text-classification
          name: "allow / cache / defer / deny on agent requests, held-out plant type"
        dataset:
          type: synthetic
          name: "Talos run5 held-out plant family and personas (synthetic; not published)"
        metrics:
          - type: accuracy
            value: 0.922
            name: "4-way action accuracy"
---

# Talos student (int8 ONNX)

The 5 MB version of Talos-0.8B. Same questions, same closed vocabulary, same calibrated output shape, distilled from the 0.8B teacher so it runs inside the gate on a CPU in about 5 ms. This is what `talos-proxy` ships with.

Code, proxy, docs: https://github.com/tractian/talos

## Model description

- **Architecture:** 6-layer, 256-wide transformer encoder, 8 heads, 4.85M parameters, over a 248-symbol vocabulary with per-question marker tokens and ten output heads.
- **Training:** knowledge distillation from Talos-0.8B's per-question distributions plus the oracle labels, then per-head temperature scaling and a conformal abstain threshold.
- **Export:** ONNX opset 17, dynamic int8 quantization. `vocab.json` and `heads.json` sit next to the model and are required.
- **Latency:** ~5 ms per request, single thread, laptop CPU.

## Intended use

Inside the Talos gate: `talos-proxy --weights <dir>` or `OnnxJudge("<dir>")` from Python. The gate's deterministic rules run first; the model can only tighten their verdict.

## Out of scope

Same as Talos-0.8B: not a safety system, not an authentication mechanism, no text interface.

## Training data

Synthetic, identical to the teacher's. Not published.

## Evaluation

Same held-out test as the teacher (15,593 requests, unseen plant family and personas).

| | student | teacher |
|---|---|---|
| 4-way action accuracy | 92.2% | 93.6% |
| accuracy with a map / without a map | 92.3% / 92.1% | 93.7% / 93.5% |
| accuracy when confident (p >= 0.9) | 97.9% (85% of requests) | 99.0% (80%) |
| misbehaving-agent AUROC | 0.991 | 0.992 |

## Files

```
student.int8.onnx
vocab.json
heads.json
provenance.json
```

`talos-pull` downloads them into `./weights`.
