# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""LLM classifier for the rows the rules are unsure about. Any OpenAI-compatible chat endpoint (`$TALOS_LLM_BASE`,
`$TALOS_LLM_MODEL`); answers cached on disk so a rebuild is reproducible offline. Only name/description/unit/type
go to the model, in batches, and the model returns JSON."""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .classify import ROLE_CLS, Verdict
from .schema import CLASSES, SAFE, TagRow

DEFAULT_URL = os.environ.get("TALOS_LLM_BASE")              # e.g. http://127.0.0.1:8000/v1 ; None = not configured
DEFAULT_MODEL = os.environ.get("TALOS_LLM_MODEL", "default")
ROLES = sorted(ROLE_CLS)

SYSTEM = (
    "You classify industrial control tags for a write-guard. For each tag decide:\n"
    "cls: one of " + ", ".join(CLASSES) + ". setpoint = operator-adjustable process target; command = start/stop/open/close/reset/mode bits "
    "and valve/output commands; tuning = gains, ramps, timers, filters; protective = trip limits, HH/LL, max/min limits, overload settings; "
    "interlock = permissives, e-stops, bypasses, lockouts; alarm_limit = alarm/warning thresholds and deadbands; config = selections, sources, "
    "scaling, addresses; diagnostic = any measured/feedback/status/counter value (read-only).\n"
    "safe_direction: down if lowering the value / writing 0 moves the equipment toward stopped, closed, cooler, slower, or makes a trip fire "
    "earlier; up if raising it is the safe way (accel/decel times, low-level trips, lockouts); none otherwise.\n"
    "wwr: true if an operator normally changes it while the equipment runs (setpoints, commands, alarm limits), false for tuning/protective/config.\n"
    "writable: false for measurements, feedbacks, statuses and inputs.\n"
    "role: one of " + ", ".join(ROLES) + " when the tag clearly plays that part for a pump/fan/compressor VFD, conveyor, tank level loop, "
    "heater loop or small reactor; else null.\n"
    "Reply with ONLY a JSON array of {i, cls, safe_direction, wwr, writable, role}."
)


class LLM:
    def __init__(self, url: str | None = DEFAULT_URL, model: str = DEFAULT_MODEL, cache_dir: str | None = None, batch: int = 40, workers: int = 8, timeout: float = 180):
        if not url:
            raise ValueError("LLM classifier needs an OpenAI-compatible base URL: pass url=... or set TALOS_LLM_BASE")
        self.url, self.model, self.batch, self.workers, self.timeout = url.rstrip("/"), model, batch, workers, timeout
        self.cache_dir = cache_dir
        self.cache: dict[str, dict] = {}
        self.cache_path = os.path.join(cache_dir, "classify.jsonl") if cache_dir else None
        if self.cache_path and os.path.exists(self.cache_path):
            with open(self.cache_path) as f:
                for line in f:
                    d = json.loads(line)
                    self.cache[d["k"]] = d["v"]
        self.calls = 0
        self.errors = 0

    @staticmethod
    def key(row: TagRow) -> str:
        return hashlib.sha1(json.dumps([row.name, row.description, row.unit, row.datatype, row.signal_type, row.range_lo, row.range_hi]).encode()).hexdigest()

    def _post(self, rows: list[TagRow]) -> list[dict]:
        lines = []
        for i, r in enumerate(rows):
            bits = [f"{i}. {r.name}"]
            if r.description:
                bits.append(f"desc={r.description!r}")
            if r.unit:
                bits.append(f"unit={r.unit}")
            if r.datatype:
                bits.append(f"type={r.datatype}")
            if r.signal_type:
                bits.append(f"io={r.signal_type}")
            if r.range_lo is not None and r.range_hi is not None:
                bits.append(f"range={r.range_lo}..{r.range_hi}")
            lines.append(" ".join(bits))
        body = {"model": self.model, "temperature": 0, "max_tokens": 60 * len(rows) + 200,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(lines)}],
                "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(self.url + "/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            d = json.load(resp)
        self.calls += 1
        txt = (d["choices"][0]["message"].get("content") or "").strip()
        if txt.startswith("```"):
            txt = txt.strip("`")
            txt = txt[4:] if txt.startswith("json") else txt
        s, e = txt.find("["), txt.rfind("]")
        return json.loads(txt[s:e + 1])

    def _one_batch(self, rows: list[TagRow]) -> dict[str, dict]:
        out = {}
        try:
            for a in self._post(rows):
                i = int(a.get("i", -1))
                if 0 <= i < len(rows):
                    out[self.key(rows[i])] = a
        except Exception as ex:   # noqa: BLE001 - one bad batch must not kill an import
            self.errors += 1
            out["__error__"] = {"err": repr(ex)[:200]}
        return out

    def classify_many(self, rows: list[TagRow]) -> dict[str, dict]:
        todo = {self.key(r): r for r in rows if self.key(r) not in self.cache}
        items = list(todo.values())
        batches = [items[i:i + self.batch] for i in range(0, len(items), self.batch)]
        new: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for res in ex.map(self._one_batch, batches):
                res.pop("__error__", None)
                new.update(res)
        if new and self.cache_path:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self.cache_path, "a") as f:
                for k, v in new.items():
                    f.write(json.dumps({"k": k, "v": v}) + "\n")
        self.cache.update(new)
        return {self.key(r): self.cache[self.key(r)] for r in rows if self.key(r) in self.cache}


def merge(rule: Verdict, ans: dict | None) -> Verdict:
    """LLM answer wins where it is well-formed; the rule verdict fills anything missing."""
    if not ans:
        return rule
    cls = ans.get("cls") if ans.get("cls") in CLASSES else rule.cls
    safe = ans.get("safe_direction") if ans.get("safe_direction") in SAFE else rule.safe_direction
    role = ans.get("role") if ans.get("role") in ROLE_CLS else None
    if role and ROLE_CLS[role] != cls and not (ROLE_CLS[role] == "diagnostic" or (cls == "diagnostic" and role in ("status_word", "fault_code", "speed_fb", "temp_fb", "current_fb", "power_fb", "level_fb", "pressure_fb", "flow_fb", "load_fb"))):
        role = None            # the model named a role that does not fit the class it chose (e.g. batch_step on a meter point)
    writable = bool(ans.get("writable", rule.writable))
    wwr = bool(ans.get("wwr", rule.wwr))
    if not writable:
        cls = "interlock" if cls == "interlock" else "diagnostic"
        safe, wwr = "none", False
    elif cls == "diagnostic":
        writable = False
    return Verdict(cls=cls, safe_direction=safe, wwr=wwr, role=role or rule.role, confidence=0.85, writable=writable, by="llm")
