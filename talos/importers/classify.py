# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Rule classifier: tag name + description + export metadata -> (cls, safe_direction, wwr, role, confidence).

Keyword rules per register class. Confidence < LLM_THRESHOLD rows are sent to the LLM (llm.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import TagRow

LLM_THRESHOLD = 0.75


@dataclass
class Verdict:
    cls: str
    safe_direction: str
    wwr: bool
    role: str | None
    confidence: float
    writable: bool
    by: str = "rule"


_READONLY_SIG = {"ai", "di", "rtd", "tc", "ai - hart", "pulse", "input", "ro", "memorydisc_ro"}
_WRITE_SIG = {"ao", "do", "output", "memoryint", "memoryreal", "memorydisc", "memorymsg"}

# (regex on lowercase text, cls, safe, wwr, role or None, confidence)
RULES: list[tuple[str, str, str, bool, str | None, float]] = [
    # interlocks / safety
    (r"\b(e[- ]?stop|emergency stop|estop|safety relay|guard|light curtain)\b", "interlock", "none", False, None, 0.9),
    (r"\b(permissive|permit|ready to start|rts)\b", "interlock", "none", False, "run_permissive", 0.85),
    (r"\b(interlock|ilk|lockout|loto|bypass)\b", "interlock", "down", True, None, 0.8),
    # protective
    (r"\b(hh|ll|high[- ]?high|low[- ]?low|trip|sis|esd|shutdown|overspeed|overload|over[- ]?current|overtemp|protection|supervision)\b", "protective", "down", False, None, 0.8),
    (r"\b(max(imum)? ?(freq\w*|speed|hz|current|torque|power|temp\w*)|current limit|torque limit|power limit|speed limit|limit)\b", "protective", "down", False, None, 0.85),
    (r"\b(min(imum)? ?(freq\w*|speed|hz))\b", "protective", "up", False, None, 0.7),
    # alarm limits
    (r"\b(alarm (limit|level|setpoint|sp)|warning (level|limit)|hi(gh)? alarm|lo(w)? alarm|alm (hi|lo|sp))\b", "alarm_limit", "down", True, None, 0.8),
    (r"\b(deadband|hysteresis)\b", "alarm_limit", "none", True, None, 0.6),
    # tuning
    (r"\b(kp|ki|kd|p[- ]?gain|i[- ]?gain|d[- ]?gain|gain|pid|proportional|integral|derivative|reset time|rate time|filter time|ramp|accel|decel|acceleration|deceleration|jam timer|timer preset|preset)\b", "tuning", "none", False, None, 0.75),
    # setpoints
    (r"\b(setpoint|set point|sp|target|reference|ref|demand|speed command|speed cmd|freq cmd|frequency command|speed ref)\b", "setpoint", "down", True, None, 0.8),
    # commands
    (r"\b(start|stop|run cmd|run command|run|open cmd|close cmd|open command|close command|enable|disable|jog|reset|ack|acknowledge|auto|manual|command|cmd|pb|pushbutton|push button)\b", "command", "down", True, None, 0.75),
    (r"\b(valve (position )?command|position command|output|out)\b", "command", "down", True, None, 0.6),
    # config
    (r"\b(mode|select|selection|source|assign|unit|scaling|scale|offset|type|config|configuration|address|baud|id)\b", "config", "none", False, None, 0.6),
    # diagnostics
    (r"\b(status|fault|alarm|warning|feedback|fb|f/b|actual|measured|pv|process value|current|temperature|temp|pressure|level|flow|speed|position|counter|count|hours|runtime|running|state|health|comm|communication|heartbeat|connection|avg|average|dev|deviation|rate)\b", "diagnostic", "none", False, None, 0.65),
]
_COMPILED = [(re.compile(p), c, s, w, r, k) for p, c, s, w, r, k in RULES]

# role hints: regex on text -> equipment role (speed_ref, run_cmd, level_fb, ...)
ROLE_HINTS = [
    (r"\b(speed|freq(uency)?)\b.*\b(ref|reference|sp|setpoint|cmd|command|demand|target)\b|\b(speed|freq) ?(ref|sp|cmd)\b|_SPD_?(SP|REF|CMD)\b|_FREQ_?(SP|REF|CMD)\b", "speed_ref"),
    (r"\b(speed|freq(uency)?|rpm)\b.*\b(fb|feedback|actual|measured|pv|f/b)\b|\b(actual|measured) (speed|freq)\b|_SPD_?FB\b", "speed_fb"),
    (r"\b(run|start)\b.*\b(cmd|command|request|req)\b|\brun_?cmd\b|\bstart_?cmd\b|_RUN@?$|_START$|\bstart\b", "run_cmd"),
    (r"\b(fault|alarm)\b.*\breset\b|_RST\b|_RESET\b|\breset\b", "fault_reset"),
    (r"\baccel", "accel_time"), (r"\bdecel", "decel_time"),
    (r"\bmax(imum)? ?(freq\w*|speed|hz|spd)\b|\bmax_?(hz|freq|spd)\b", "max_freq"),
    (r"\bcurrent limit\b|\bi ?limit\b|\bcurr_?lim\b", "current_limit"),
    (r"\b(high|hi|hh)\b.*\btemp\w*\b.*\b(trip|hh)\b|\btemp\w*\b.*\b(hh|trip)\b|\btshh\b", "high_temp_trip"),
    (r"\b(high|hi)\b.*\btemp\w*\b.*\b(alarm|alm)\b|\btemp\w*\b.*\b(alarm|alm)\b.*\b(limit|level|sp|setpoint)\b|\btah\b|\btsh\b", "high_temp_alarm"),
    (r"\btemp(erature)?\b.*\b(sp|setpoint|set point|target|demand)\b|_TEMP_?SP\b", "temp_sp"),
    (r"\b(motor|winding|bearing|oil|discharge|jacket)?\s*temp(erature)?\b", "temp_fb"),
    (r"\b(motor )?current\b.*\b(fb|f/b|feedback|actual|measured|pv|avg)\b|\b(motor )?current\b|_AMPS?\b|\bamps\b", "current_fb"),
    (r"\blevel\b.*\b(sp|setpoint|set point|target)\b|_LVL_?SP\b", "level_sp"),
    (r"\blevel\b|\btank (remaining )?inches\b|_LVL\b|_LWATER\b|\blt[-_ ]?\d", "level_fb"),
    (r"\bpressure\b.*\b(hh|high[- ]?high|trip)\b|\bpshh\b", "high_pressure_trip"),
    (r"\bpressure\b.*\b(alarm|hi|high)\b|\bpah\b|\bpsh\b", "high_pressure_alarm"),
    (r"\b(suction |discharge )?pressure\b|_PRESS\b|\bpt[-_ ]?\d|_PSUCTION\b|_PDISCH", "pressure_fb"),
    (r"\bflow\b.*\b(sp|setpoint)\b", "level_sp"),
    (r"\bflow\b|\bft[-_ ]?\d|\bfit[-_ ]?\d", "flow_fb"),
    (r"\b(inlet|supply|feed|fill)\b.*\bvalve\b|\bfeed valve\b", "feed_valve_cmd"),
    (r"\bvent\b", "vent_cmd"),
    (r"\bpower limit\b|\bheater (power )?limit\b", "power_limit"),
    (r"\b(heater|heat)\b.*\b(enable|on|cmd|command)\b", "heater_enable"),
    (r"\bpower\b.*\b(fb|feedback|actual|kw|output)\b|\bkw\b|\bpower\b", "power_fb"),
    (r"\b(load|torque)\b.*\b(fb|feedback|actual|%|percent)?\b", "load_fb"),
    (r"\bstatus( word)?\b|\bsts\b|\bstatus\b", "status_word"),
    (r"\bfault( code)?\b|\bflt\b|\bthermal fault\b|\btrip(ped)?\b", "fault_code"),
    (r"\b(zone )?bypass\b", "zone_bypass"), (r"\bjam\b", "jam_timer"), (r"\btorque limit\b", "torque_limit"),
    (r"\b(fwd|rev|forward|reverse|direction|dir)\b", "direction"), (r"\bjog\b", "jog"),
    (r"\bmanual\b.*\bmode\b|\bauto/?man\b|\bhand\b", "manual_mode"),
    (r"\b(pid|loop)\b.*\b(gain|kp|prop)\b|\bgain\b", "pid_gain"), (r"\b(reset|integral|ti)\b.*\btime\b|\breset time\b", "pid_reset"),
    (r"\bpermissive\b|\bready\b", "run_permissive"), (r"\be[- ]?stop\b|\bestop\b", "estop_ok"),
    (r"\blockout\b|\bloto\b", "lockout"), (r"\binterlock bypass\b|\bbypass\b", "interlock_bypass"),
    (r"\b(outlet|drain|discharge)\b.*\bvalve\b", "outlet_valve_cmd"), (r"\bvalve\b", "inlet_valve_cmd"),
    (r"\b(pump|agitator|mixer|fan|blower|compressor|motor)\b.*\b(run|running|on|start)\b", "run_cmd"),
    (r"\bstep\b|\bphase\b|\brecipe\b", "batch_step"),
]
_ROLE_HINTS = [(re.compile(p, re.I), r) for p, r in ROLE_HINTS]

# roles that are written (device inputs); others are feedback
WRITE_ROLES = {"speed_ref", "run_cmd", "fault_reset", "accel_time", "decel_time", "max_freq", "current_limit", "temp_sp", "level_sp",
               "high_pressure_trip", "high_pressure_alarm", "feed_valve_cmd", "vent_cmd", "high_temp_trip", "high_temp_alarm",
               "power_limit", "heater_enable", "zone_bypass", "jam_timer", "torque_limit", "direction", "jog", "manual_mode",
               "pid_gain", "pid_reset", "lockout", "interlock_bypass", "outlet_valve_cmd", "inlet_valve_cmd", "batch_step",
               "agitator_run", "agitator_speed_ref", "jacket_temp_sp", "pump_run", "low_level_trip", "high_level_alarm", "comm_timeout", "control_place"}
ROLE_CLS = {"speed_ref": "setpoint", "temp_sp": "setpoint", "level_sp": "setpoint", "jacket_temp_sp": "setpoint", "agitator_speed_ref": "setpoint",
            "run_cmd": "command", "fault_reset": "command", "feed_valve_cmd": "command", "vent_cmd": "command", "heater_enable": "command",
            "direction": "command", "jog": "command", "outlet_valve_cmd": "command", "inlet_valve_cmd": "command", "pump_run": "command", "agitator_run": "command",
            "accel_time": "tuning", "decel_time": "tuning", "jam_timer": "tuning", "pid_gain": "tuning", "pid_reset": "tuning",
            "max_freq": "protective", "current_limit": "protective", "high_pressure_trip": "protective", "high_temp_trip": "protective",
            "power_limit": "protective", "torque_limit": "protective", "low_level_trip": "protective", "comm_timeout": "protective",
            "high_pressure_alarm": "alarm_limit", "high_temp_alarm": "alarm_limit", "high_level_alarm": "alarm_limit",
            "manual_mode": "config", "batch_step": "config", "control_place": "config",
            "zone_bypass": "interlock", "lockout": "interlock", "interlock_bypass": "interlock", "run_permissive": "interlock", "estop_ok": "interlock",
            "speed_fb": "diagnostic", "temp_fb": "diagnostic", "current_fb": "diagnostic", "power_fb": "diagnostic", "level_fb": "diagnostic",
            "pressure_fb": "diagnostic", "flow_fb": "diagnostic", "load_fb": "diagnostic", "status_word": "diagnostic", "fault_code": "diagnostic"}


def _norm(s: str) -> str:
    s = re.sub(r"[_\.\-:/\\\[\]\(\)@#]+", " ", s)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)   # camelCase -> camel Case
    return s.lower()


def is_readonly(row: TagRow) -> bool | None:
    """True/False when the export says so, None when it does not."""
    a = (row.access or "").lower()
    if a in ("ro", "r", "read", "readonly", "read-only"):
        return True
    if a in ("rw", "wo", "w", "write", "readwrite", "read/write"):
        return False
    st = (row.signal_type or "").lower().strip()
    if st in _READONLY_SIG:
        return True
    if st in _WRITE_SIG:
        return False
    k = (row.address or "").lower()
    if re.search(r":i\.|%i|^3\d{4}$|^1\d{4}$|\bir\b|\bdi\b", k):
        return True
    return None


def role_for(row: TagRow) -> str | None:
    text = _norm(row.text)
    for rx, role in _ROLE_HINTS:
        if rx.search(text) or rx.search(row.name):
            return role
    return None


def classify(row: TagRow) -> Verdict:
    text = _norm(row.text)
    ro = is_readonly(row)
    role = role_for(row)
    best: tuple[str, str, bool, float] | None = None
    for rx, cls, safe, wwr, _r, conf in _COMPILED:
        if rx.search(text):
            if best is None or conf > best[3]:
                best = (cls, safe, wwr, conf)
    if best is None:
        best = ("diagnostic" if ro else "config", "none", False, 0.3)
    cls, safe, wwr, conf = best
    if ro is True and cls not in ("interlock",):
        cls, safe, wwr = "diagnostic", "none", False
        conf = max(conf, 0.8)
    if ro is False and cls == "diagnostic":
        cls, conf = ("command" if (row.datatype or "").lower().startswith("bool") else "setpoint"), 0.5
        safe, wwr = "down", True
    if role and role in ROLE_CLS and ROLE_CLS[role] != "diagnostic" and ro is not True:
        rc = ROLE_CLS[role]
        if rc != cls:
            conf = min(conf, 0.6) if conf >= 0.75 else conf   # the rule and the role disagree: let the LLM look
        cls = rc if conf < 0.75 else cls
        safe = {"setpoint": "down", "command": "down", "protective": "down", "alarm_limit": "down", "tuning": "none", "config": "none", "interlock": "up", "diagnostic": "none"}[cls]
        wwr = cls in ("setpoint", "command", "alarm_limit", "interlock")
    if role and role not in WRITE_ROLES and ro is not False:
        cls, safe, wwr = ("interlock" if role in ("run_permissive", "estop_ok") else "diagnostic"), "none", False
        ro = True
    if role in ("accel_time", "decel_time", "lockout", "low_level_trip"):
        safe = "up"
    writable = (ro is False) or (ro is None and cls != "diagnostic")
    if not writable:
        cls = "interlock" if cls == "interlock" else "diagnostic"
        safe, wwr = "none", False
    return Verdict(cls=cls, safe_direction=safe, wwr=wwr, role=role, confidence=round(conf, 2), writable=writable)
