# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
import json
import os

import pytest

from talos.importers.classify import classify, role_for
from talos.importers.llm import merge
from talos.importers.plantmap import build_plant_map, group_key, vote_kind
from talos.importers.schema import TagRow, guess_scale, parse_address
from talos.importers.tagexport import csv_rows, logix_rows
from talos.mapformat import validate

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "tag_export_fixture.csv")


def test_parse_address_notations():
    assert parse_address("40001") == (0, "hr")
    assert parse_address("30011") == (10, "ir")
    assert parse_address("00002") == (1, "co")
    assert parse_address("10041") == (40, "di")
    assert parse_address("400123") == (122, "hr")
    assert parse_address("%MW12") == (12, "hr")
    assert parse_address("%IX0.3") == (3, "di")
    assert parse_address("%QX1.2") == (10, "co")
    assert parse_address("HR:7") == (7, "hr")
    assert parse_address("4x:12") == (12, "hr")
    assert parse_address("Local:1:I.Data.3", "ir") == (None, "ir")
    assert parse_address("", "co") == (None, "co")


def test_guess_scale_fits_uint16():
    assert guess_scale("Hz", 0, 60) == 100.0
    assert guess_scale("C", 0, 700) == 10.0
    assert guess_scale("rpm", 0, 30000) == 1.0
    assert guess_scale("", 0, 1, "Bool") == 1.0


def test_rule_classifier_on_fixture():
    rows = list(csv_rows(FIX))
    assert len(rows) == 30
    v = {r.name: classify(r) for r in rows}
    assert v["P-101_SPD_SP"].cls == "setpoint" and v["P-101_SPD_SP"].safe_direction == "down" and v["P-101_SPD_SP"].wwr
    assert v["P-101_SPD_FB"].cls == "diagnostic" and not v["P-101_SPD_FB"].writable
    assert v["P-101_RUN_CMD"].cls == "command" and v["P-101_RUN_CMD"].role == "run_cmd"
    assert v["P-101_ACCEL"].cls == "tuning" and v["P-101_ACCEL"].safe_direction == "up" and not v["P-101_ACCEL"].wwr
    assert v["P-101_MAX_HZ"].cls == "protective" and v["P-101_MAX_HZ"].role == "max_freq"
    assert v["P-101_READY"].cls == "interlock" and not v["P-101_READY"].writable
    assert v["TK-201_HH_TRIP"].cls == "protective" and not v["TK-201_HH_TRIP"].wwr
    assert v["TK-201_PID_KP"].cls == "tuning"
    assert v["E-301_TSHH"].cls == "protective"
    assert v["C-401_ESTOP_OK"].cls == "interlock" and not v["C-401_ESTOP_OK"].writable
    assert v["C-401_JAM_T"].cls == "tuning"
    assert v["P-102_HI_TEMP_ALM"].cls == "alarm_limit"
    # read-only exports never come out writable, whatever the name says
    assert all(not x.writable for n, x in v.items() if n.endswith(("_FB", "_AMPS", "_LVL", "_TEMP")))


def test_roles_and_asset_kinds():
    rows = list(csv_rows(FIX))
    assert role_for(next(r for r in rows if r.name == "P-101_SPD_SP")) == "speed_ref"
    assert role_for(next(r for r in rows if r.name == "TK-201_LVL")) == "level_fb"
    assert role_for(next(r for r in rows if r.name == "E-301_HEAT_EN")) == "heater_enable"
    groups = {}
    for r in rows:
        groups.setdefault(group_key(r), []).append(r)
    kinds = {k: vote_kind(g, k)[0] for k, g in groups.items()}
    assert kinds == {"P-101": "vfd_pump", "P-102": "vfd_pump", "TK-201": "tank_level_loop", "E-301": "heater_loop", "C-401": "conveyor"}


def test_build_family_template_hashes_everything():
    build_family = pytest.importorskip("talos.importers.families", reason="private data-engine module").build_family
    rows = list(csv_rows(FIX))
    fam = build_family("fixture_plant", rows, "fixture-src", llm=None, max_assets=8)
    assert fam["schema"] == "talos.family/1" and len(fam["assets"]) == 5
    dumped = json.dumps(fam)
    for leak in ("P-101", "Day tank", "Preheater", "Conveyor", "TK-201"):
        assert leak not in dumped
    pump = next(a for a in fam["assets"] if a["kind"] == "vfd_pump" and a["n_tags"] == 8)
    assert {"speed_ref", "speed_fb", "run_cmd", "current_fb", "accel_time", "max_freq", "fault_reset", "run_permissive"} <= set(pump["roles"])
    assert pump["roles"]["speed_ref"]["limits"] == [0.0, 60.0] and pump["roles"]["speed_ref"]["scale"] == 100.0
    assert pump["roles"]["speed_fb"]["writable"] is False
    tank = next(a for a in fam["assets"] if a["kind"] == "tank_level_loop")
    assert tank["roles"]["level_fb"]["envelope"] == [10.0, 90.0]


def test_build_plant_map_validates_and_keeps_addresses():
    rows = list(csv_rows(FIX))
    m, unaddressed = build_plant_map(rows)
    validate(m)
    assert m["layer"] == "plant" and unaddressed == []
    by = {(r["kind"], r["address"]): r for r in m["registers"]}
    assert by[("hr", 0)]["cls"] == "setpoint" and by[("hr", 0)]["access"] == "rw"
    assert by[("ir", 0)]["cls"] == "diagnostic" and by[("ir", 0)]["access"] == "ro"
    assert by[("co", 0)]["cls"] == "command"
    assert by[("di", 0)]["cls"] == "interlock"
    assert all(len(r["name"]) == 10 for r in m["registers"])
    assert "P-101" not in json.dumps(m)


def test_plant_map_synthesises_addresses_when_missing():
    rows = [TagRow(name="FIC-101.SP", description="flow setpoint", unit="m3/h", range_lo=0, range_hi=100, access="rw"),
            TagRow(name="FIC-101.PV", description="flow", unit="m3/h", range_lo=0, range_hi=100, access="ro"),
            TagRow(name="FIC-101.KP", description="PID gain", access="rw")]
    m, unaddressed = build_plant_map(rows)
    validate(m)
    assert len(unaddressed) == 3
    assert {r["kind"] for r in m["registers"]} == {"hr", "ir"}


def test_logix_export_adapter(tmp_path):
    p = tmp_path / "tags.csv"
    p.write_text('remark,"CSV-Import-Export"\nTYPE,SCOPE,NAME,DESCRIPTION,DATATYPE,SPECIFIER,ATTRIBUTES\n'
                 'TAG,,Pump1_SpeedRef,"Pump 1 speed reference",REAL,,"(RADIX := Float, ExternalAccess := Read/Write)"\n'
                 'TAG,,Pump1_Amps,"Pump 1 motor amps",REAL,,"(RADIX := Float, ExternalAccess := Read Only)"\n'
                 'ALIAS,,Pump1_Run,"Pump 1 run",BOOL,Local:2:O.Data.0,"(RADIX := Decimal, ExternalAccess := Read/Write)"\n')
    rows = list(logix_rows(str(p)))
    assert [r.name for r in rows] == ["Pump1_SpeedRef", "Pump1_Amps", "Pump1_Run"]
    assert rows[1].access == "ro" and rows[0].access == "rw"
    v = [classify(r) for r in rows]
    assert v[0].cls == "setpoint" and v[1].cls == "diagnostic" and v[2].cls == "command"


def test_llm_merge_respects_readonly_and_bad_answers():
    r = TagRow(name="X", access="ro")
    rule = classify(r)
    out = merge(rule, {"cls": "setpoint", "safe_direction": "sideways", "wwr": True, "writable": False, "role": "nope"})
    assert out.cls == "diagnostic" and out.safe_direction == "none" and not out.wwr and out.by == "llm"
    assert merge(rule, None) is rule


def test_parker_fields_and_map():
    parker = pytest.importorskip("talos.importers.parker", reason="private data-engine module")
    _parse_fields, build_map = parker._parse_fields, parker.build_map
    sp = _parse_fields(["0", "23/s,-10000,10000,0.01,1.2;%.2fHz;(8) Preset Speed 1 (Hz)"])
    assert sp == {"tag": 23, "ro": False, "type": "s", "lo": -100.0, "hi": 100.0, "res": 0.01, "unit": "Hz", "enum": None}
    sp2 = _parse_fields(["0", "222/:s,-30000,30000,0.01;%.2f%%;(207) Speed Feedback (%)"])
    assert sp2["ro"] and sp2["unit"] == "%"
    sp3 = _parse_fields(["0", "28/ev,0 .. 10v,2 .. 10v;(13) Type"])
    assert sp3["type"] == "ev" and sp3["hi"] == 1.0
    params = {8: {"name": "Preset Speed 1 (Hz)", "block": "Preset Speeds", "spec": sp},
              207: {"name": "Speed Feedback (%)", "block": "Diagnostics", "spec": sp2},
              13: {"name": "Type", "block": "Analog Input 1", "spec": sp3},
              40: {"name": "Accel Time", "block": "Reference Ramp", "spec": _parse_fields(["0", "258/s,1,30000,0.1;%.1fs;(40) Accel Time"])}}
    m = build_map("690", params)
    validate(m)
    assert m["device"]["vendor"] == "parker_ssd" and m["device"]["family"] == "690plus"
    by = {r["param"]: r for r in m["registers"]}
    assert by["207"]["access"] == "ro" and by["207"]["cls"] == "diagnostic" and by["207"]["address"] == 222
    assert by["40"]["cls"] == "tuning" and by["40"]["safe_direction"] == "up" and by["40"]["scale"] == 10.0
    assert by["13"]["cls"] == "config" and by["13"]["enum"]
    assert "accel_time" in m["roles"] and m["roles"]["accel_time"] == 258


def test_cli_map_on_fixture(tmp_path):
    from talos.importers.__main__ import main
    out = tmp_path / "m.json"
    main(["map", "--from", f"iolist:{FIX}", "--out", str(out)])
    validate(json.load(open(out)))
