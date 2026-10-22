# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
from pathlib import Path

from talos.gate import Gate, GateConfig
from talos.ledger import Ledger
from talos.model import JudgeTimeout, NullJudge
from talos.schema import Action, Op, Protocol, Request, Target, Tier
from talos.world import PlantState, World

FIX = Path(__file__).parent / "fixtures" / "world.json"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


class FixedJudge:
    """Returns a fixed answer set (per-question) for every request."""
    name = "fixed"

    def __init__(self, action=None, **nouls):
        self.action = action or {"allow": 1.0, "cache": 0.0, "defer": 0.0, "deny": 0.0}
        self.nouls = nouls
        self.calls = 0

    def judge(self, body, deadline_s=None):
        self.calls += 1
        out = {"action": dict(self.action), "envelope": {"inside_typical": 1.0}, "severity": {"score": 0.0, "probabilities": {"0": 1.0}}}
        for q in ("needed_now", "duplicate", "servable", "over_budget", "contradiction", "state_conflict", "misbehaving"):
            out[q] = self.nouls.get(q, 0.0)
        return out


class TimeoutJudge:
    name = "timeout"

    def judge(self, body, deadline_s=None):
        raise JudgeTimeout("deadline")


def mk_gate(judge=None, clock=None, **cfg):
    clock = clock or Clock()
    world = World.load(FIX)
    g = Gate(GateConfig(**cfg), world=world, judge=judge or FixedJudge(), clock=clock)
    g.registry.register("a:good", role="copilot", tier=Tier.normal, now=clock())
    g.registry.register("a:eng", role="engineering", tier=Tier.trusted, now=clock())
    g.registry.register("a:q", role="copilot", tier=Tier.quarantine, now=clock())
    return g, clock


def plant(speed=4000.0, state="running", **kw):
    return PlantState.from_dict({"tags": {"t:pump1.speed_ref": {"value": speed, "age_s": 0.2}, "t:pump1.speed_fb": {"value": speed, "age_s": 0.3}},
                                 "assets": {"pump1": state, "fan1": "running"}, **kw})


def req(agent="a:good", op=Op.write, tag="t:pump1.speed_ref", value=4200.0, **kw):
    return Request(agent_id=agent, protocol=Protocol.modbus, op=op, target=Target(tag=tag), value=value, **kw)


def test_normal_write_allowed_by_model():
    g, c = mk_gate()
    d = g.decide(req(), plant())
    assert d.action == Action.allow and d.by == "model"
    assert d.ledger_id


def test_trap_denies_and_quarantines():
    g, c = mk_gate()
    d = g.decide(req(tag="t:pump1.trap_a", value=1), plant())
    assert d.action == Action.deny and d.by == "traps"
    assert g.registry.state("a:good").tier == Tier.quarantine
    d2 = g.decide(req(tag="t:pump1.trap_a", op=Op.read, value=None), plant())
    assert d2.action == Action.deny and g.registry.state("a:good").tier == Tier.blocked


def test_hard_clamp_denies_without_model():
    j = FixedJudge()
    g, c = mk_gate(judge=j)
    d = g.decide(req(value=9000.0), plant())
    assert d.action == Action.deny and d.by == "fastpath" and "fastpath:hard_clamp" in d.reasons
    assert j.calls == 0


def test_allowlist_normal_cannot_write_protective():
    g, c = mk_gate()
    d = g.decide(req(tag="t:pump1.max_freq", value=5000.0), plant(state="stopped"))
    assert d.action == Action.deny and any(r.startswith("fastpath:allowlist") for r in d.reasons)
    d2 = g.decide(req(agent="a:eng", tag="t:pump1.max_freq", value=5000.0), plant(state="stopped"))
    assert d2.action == Action.allow


def test_not_writable_while_running():
    g, c = mk_gate()
    d = g.decide(req(agent="a:eng", tag="t:pump1.max_freq", value=5000.0), plant(state="running"))
    assert d.action == Action.deny and "fastpath:not_writable_while_running" in d.reasons


def test_model_cannot_loosen_fastpath():
    g, c = mk_gate(judge=FixedJudge(action={"allow": 1.0, "cache": 0, "defer": 0, "deny": 0}))
    d = g.decide(req(value=9000.0), plant())
    assert d.action == Action.deny


def test_model_can_tighten():
    g, c = mk_gate(judge=FixedJudge(action={"allow": 0.1, "cache": 0, "defer": 0.0, "deny": 0.9}))
    d = g.decide(req(), plant())
    assert d.action == Action.deny and d.by == "model"


def test_timeout_never_allows():
    g, c = mk_gate(judge=TimeoutJudge())
    d = g.decide(req(), plant())
    assert d.action != Action.allow and d.by == "fallback" and d.action == Action.defer
    # reads fall back to cache when a value is known
    d2 = g.decide(req(op=Op.read, tag="t:pump1.speed_fb", value=None), plant())
    assert d2.action in (Action.cache, Action.defer) and d2.action != Action.allow
    assert g.stats["model_failures"] >= 1


def test_stop_bypass_on_safe_direction_skips_model():
    j = FixedJudge(action={"allow": 0, "cache": 0, "defer": 0, "deny": 1.0})
    g, c = mk_gate(judge=j)
    d = g.decide(req(value=0.0), plant())          # speed down toward 0 = safe direction
    assert d.action == Action.allow and d.by == "fastpath" and "fastpath:stop_bypass" in d.reasons
    assert j.calls == 0
    # even a quarantined agent's stop passes with bypass on
    d2 = g.decide(req(agent="a:q", value=0.0), plant())
    assert d2.action == Action.allow


def test_stop_bypass_off_only_quarantine_deniable():
    j = FixedJudge(action={"allow": 0, "cache": 0, "defer": 0, "deny": 1.0})
    g, c = mk_gate(judge=j, stop_bypass=False)
    d = g.decide(req(value=0.0), plant())          # normal tier: model says deny, policy protects the stop
    assert d.action == Action.allow and "safe_direction:protected" in d.reasons and j.calls == 1
    d2 = g.decide(req(agent="a:q", value=0.0), plant())
    assert d2.action == Action.deny


def test_budget_defers_with_retry():
    g, c = mk_gate()
    out = []
    for i in range(40):
        out.append(g.decide(req(value=4000.0 + i, op=Op.write), plant()).action)   # normal write budget 30/min, capacity 5
    assert Action.defer in out
    d = g.decide(req(value=4500.0), plant())
    assert d.action == Action.defer and d.retry_after_ms and d.retry_after_ms > 0
    c.tick(120)
    assert g.decide(req(value=4500.0), plant()).action == Action.allow


def test_read_served_from_cache_when_fresh():
    g, c = mk_gate()
    g.decide(req(op=Op.read, tag="t:pump1.speed_fb", value=None), plant())
    g.observe_read("t:pump1.speed_fb", 4000.0)
    d2 = g.decide(req(op=Op.read, tag="t:pump1.speed_fb", value=None), PlantState())
    assert d2.action == Action.cache and d2.by == "cache"
    c.tick(10)
    d3 = g.decide(req(op=Op.read, tag="t:pump1.speed_fb", value=None), PlantState())
    assert d3.by in ("model", "fastpath")


def test_decision_cache_replays():
    j = FixedJudge()
    g, c = mk_gate(judge=j)
    g.decide(req(value=4100.0), plant())
    n = j.calls
    d = g.decide(req(value=4100.0), plant())
    assert d.by == "cache" and j.calls == n


def test_drift_accumulator_flags_and_caps(tmp_path):
    g, c = mk_gate()
    g.ledger = Ledger(tmp_path / "l.jsonl")
    v = 3000.0
    last = None
    for i in range(12):
        v += 100.0
        c.tick(10)
        last = g.decide(req(value=v), plant(speed=v - 100.0))
        if last.action == Action.allow:
            g.observe_write("t:pump1.speed_ref", v)
    g.ledger.close()
    row = list(g.ledger.iter_rows())
    # the state text carries the drift word once enough same-sign writes accumulate
    assert any("drift=up" in (r.get("state") or "") for r in row)


def test_quarantine_hysteresis_and_read_only():
    g, c = mk_gate(judge=FixedJudge(action={"allow": 0.3, "cache": 0, "defer": 0.6, "deny": 0.1}, misbehaving=0.9))
    for i in range(6):   # the first write equals the fresh current value and is acked deterministically (no model call)
        c.tick(3)
        g.decide(req(value=4000.0 + 10 * i, agent="a:good"), plant())
    assert g.registry.state("a:good").tier == Tier.quarantine
    d = g.decide(req(value=4300.0), plant())
    assert d.action == Action.defer and "fastpath:quarantine:read_only" in d.reasons


def test_unknown_write_denied_unknown_read_flagged():
    g, c = mk_gate()
    d = g.decide(req(tag="t:nope", value=1.0), plant())
    assert d.action == Action.deny
    d2 = g.decide(req(tag="t:nope", op=Op.read, value=None), plant())
    assert d2.action != Action.deny


def test_unregistered_source_gets_synthetic_id():
    g, c = mk_gate()
    r = req(agent="x")
    d = g.decide(r, plant(), source={"ip": "10.0.0.9", "unit": 1})
    assert r.agent_id.startswith("u:") and d.action in (Action.allow, Action.defer)


def test_shadow_mode_returns_allow_but_logs():
    g, c = mk_gate(judge=FixedJudge(action={"allow": 0, "cache": 0, "defer": 0, "deny": 1.0}), shadow=True)
    d = g.decide(req(), plant())
    assert d.action == Action.allow and d.reasons[0] == "shadow:would=deny"


def test_no_free_strings_in_state(tmp_path):
    g, c = mk_gate()
    g.ledger = Ledger(tmp_path / "l.jsonl")
    g.decide(req(), plant())
    g.ledger.close()
    rows = list(g.ledger.iter_rows())
    s = rows[-1]["state"]
    assert "t:pump1" not in s and "a:good" not in s and "10.0" not in s
    assert all("=" in tok for tok in s.split("\n")[0].split())


def test_ledger_chain(tmp_path):
    path = tmp_path / "ledger.jsonl"
    world = World.load(FIX)
    g = Gate(GateConfig(), world=world, judge=FixedJudge(), ledger=Ledger(path))
    for i in range(5):
        g.decide(req(value=4000.0 + i), plant())
    g.ledger.close()
    assert Ledger(path).verify() == (True, None)
    lines = path.read_text().splitlines()
    lines[2] = lines[2].replace('"action":"allow"', '"action":"deny"')
    path.write_text("\n".join(lines) + "\n")
    ok, n = Ledger(path).verify()
    assert ok is False and n == 2


def test_flood_quarantines_without_model():
    """A flooder the fastpath keeps deferring for budget lands in quarantine even when the model never answers."""
    g, c = mk_gate(judge=NullJudge())
    tiers = []
    for i in range(400):
        d = g.decide(req(op=Op.read, tag="t:pump1.speed_fb", value=None), plant())
        tiers.append(d.tier)
        c.tick(0.01)
    assert Tier.quarantine in tiers
    assert g.registry.agents["a:good"].tier == Tier.quarantine


def test_slightly_over_budget_is_not_flood():
    g, c = mk_gate(judge=NullJudge())
    for i in range(300):                       # 4/s against a 120/min read budget: deferred often, not a flood
        g.decide(req(op=Op.read, tag="t:pump1.speed_fb", value=None), plant())
        c.tick(0.25)
    assert g.registry.agents["a:good"].tier != Tier.quarantine


def test_trusted_read_goes_through_model_by_default():
    # D19: humans are judged like everyone else; the cache still answers fresh reads, the model answers the rest
    j = FixedJudge()
    g, c = mk_gate(judge=j)
    for i in range(5):
        d = g.decide(req(agent="a:eng", op=Op.read, tag="t:pump1.speed_fb", value=None), PlantState())
        c.tick(3)   # past the value TTL so the cache cannot answer
        assert d.by == "model"
    assert j.calls == 5


def test_trusted_read_fast_lane_is_opt_in():
    j = FixedJudge()
    g, c = mk_gate(judge=j)
    g.cfg.trusted_reads_skip_model = True
    for i in range(5):
        d = g.decide(req(agent="a:eng", op=Op.read, tag="t:pump1.speed_fb", value=None), PlantState())
        c.tick(3)
        assert d.action == Action.allow and d.by == "fastpath"
    assert j.calls == 0
    d = g.decide(req(agent="a:good", op=Op.read, tag="t:pump1.speed_fb", value=None), PlantState())
    assert j.calls == 1   # a normal-tier read still consults the model
