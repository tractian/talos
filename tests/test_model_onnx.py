# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""The trained student, loaded as OnnxJudge, through the live Gate (render_v01 tokens).

Needs the weights (student.int8.onnx + vocab.json + heads.json) in $TALOS_WEIGHTS or ./weights (`talos-pull`); skips otherwise.
"""
import os
from pathlib import Path

import pytest

from talos.gate import Gate, GateConfig
from talos.model import JudgeError, OnnxJudge
from talos.render_v01 import pack
from talos.schema import Op, Protocol, Request, Target, Tier
from talos.weights import has_weights
from talos.world import PlantState, World

FIX = Path(__file__).parent / "fixtures" / "world.json"
WEIGHTS = Path(os.environ.get("TALOS_WEIGHTS", "weights"))

pytestmark = pytest.mark.skipif(not has_weights(WEIGHTS), reason=f"student weights not present in {WEIGHTS} (set TALOS_WEIGHTS or run talos-pull)")


def _judge():
    try:
        return OnnxJudge(str(WEIGHTS))
    except JudgeError as e:
        pytest.skip(str(e))


def _gate():
    g = Gate(GateConfig(), world=World.load(FIX), judge=_judge())
    g.registry.register("a:good", role="copilot", tier=Tier.normal, now=0.0)
    return g


def _plant(speed=4000.0):
    return PlantState.from_dict({"tags": {"t:pump1.speed_ref": {"value": speed, "age_s": 0.2}, "t:pump1.speed_fb": {"value": speed, "age_s": 0.3}},
                                 "assets": {"pump1": "running", "fan1": "running"}})


def test_judge_answers_shape():
    j = _judge()
    assert j.wants == "render_v01" and j.inputs == ["ids"]
    a = j.judge_tokens(["OP=write", "PROTO=modbus", "CLS=setpoint"])
    assert abs(sum(a["action"].values()) - 1.0) < 1e-6 and set(a["action"]) == {"allow", "cache", "defer", "deny"}
    assert 0.0 <= a["misbehaving"] <= 1.0 and 0.0 <= a["severity"]["score"] <= 4.0
    assert j.encode(["OP=write"]).tolist()[0] == pack(["OP=write"])


def test_gate_write_and_read_reach_the_model():
    g = _gate()
    d = g.decide(Request(agent_id="a:good", protocol=Protocol.modbus, op=Op.write, target=Target(tag="t:pump1.speed_ref"), value=4200.0), _plant())
    assert d.by == "model" and abs(sum(d.p.values()) - 1.0) < 1e-3
    d2 = g.decide(Request(agent_id="a:good", protocol=Protocol.modbus, op=Op.read, target=Target(tag="t:pump1.speed_fb"), value=None), _plant())
    assert d2.by in ("model", "cache", "fastpath") and d2.action is not None
    # strings never reach the model: every token the gate produced is in the student's vocabulary
    assert g.stats.get("model_failures", 0) == 0
