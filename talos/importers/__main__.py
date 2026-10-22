# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""CLI for the import route (console script `talos-importer`, or `python -m talos.importers`).

  talos-importer map --from iolist:PATH|csv:PATH|logix:PATH|ignition:PATH --out map.json [--llm URL|none] [--cache DIR]

The `families` and `parker` subcommands belong to the private data engine and only work in the lab checkout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .llm import LLM
from .tagexport import ADAPTERS


def _llm(a):
    if a.llm in ("none", "", None):
        return None
    return LLM(url=a.llm, cache_dir=a.cache, batch=a.batch, workers=a.workers)


def cmd_map(a):
    from .plantmap import build_plant_map
    kind, _, src = a.src.partition(":")
    if kind not in ADAPTERS:
        raise SystemExit(f"--from must be one of {'|'.join(sorted(ADAPTERS))}:PATH, got {a.src!r}")
    rows = list(ADAPTERS[kind](src))
    if a.max_rows:
        rows = rows[:a.max_rows]
    m, unaddressed = build_plant_map(rows, _llm(a))
    with open(a.out, "w") as f:
        json.dump(m, f, indent=1)
    by = {}
    for r in m["registers"]:
        by[r["classified_by"]] = by.get(r["classified_by"], 0) + 1
    print(json.dumps({"rows": len(rows), "registers": len(m["registers"]), "unaddressed": len(unaddressed), "classified_by": by}))


def cmd_families(a):
    from .families import build_family  # private: simulator family templates
    from .parker import seed_rows
    pairs = [s.split("=", 1) for s in a.sources]
    srcs = [p[0] for p in pairs]
    names = {p[0]: (p[1] if len(p) > 1 else p[0]) for p in pairs}
    rows = seed_rows(a.seed_tags, srcs, a.max_rows)
    llm = _llm(a)
    os.makedirs(a.out, exist_ok=True)
    for s in srcs:
        fam = build_family(names[s], rows[s], s, llm, max_assets=a.max_assets)
        with open(os.path.join(a.out, f"{names[s]}.json"), "w") as f:
            json.dump(fam, f, indent=1)
        print(json.dumps({"family": names[s], "rows": fam["n_rows"], "groups": fam["n_groups"], "assets": [(x["kind"], x["n_tags"], len(x["roles"]), len(x["extra"])) for x in fam["assets"]],
                          "stats": fam["stats"]}))
    if llm:
        print(json.dumps({"llm_calls": llm.calls, "llm_errors": llm.errors}), file=sys.stderr)


def cmd_parker(a):
    from .parker import build_map, collect  # private: vendor maps from drive parameter tables
    per = collect(a.seed_tags)
    os.makedirs(a.out, exist_ok=True)
    for fam, params in sorted(per.items()):
        m = build_map(fam, params)
        path = os.path.join(a.out, f"parker_ssd_{m['device']['family']}.json")
        with open(path, "w") as f:
            json.dump(m, f, indent=1)
        print(json.dumps({"family": fam, "params": len(params), "registers": len(m["registers"]), "roles": m["roles"], "path": path}))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="talos-importer", description="Turn a tag export into a Talos map.")
    ap.add_argument("--llm", default="none", help="OpenAI-compatible base URL for the uncertain rows (or $TALOS_LLM_BASE); 'none' = rules only")
    ap.add_argument("--cache", default="./talos-llm-cache", help="directory for cached LLM answers (default ./talos-llm-cache)")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-rows", type=int, default=0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("map", help="tag export -> plant map (talos.map/1)")
    p.add_argument("--from", dest="src", required=True, help="iolist:PATH | csv:PATH | logix:PATH | ignition:PATH")
    p.add_argument("--out", required=True); p.set_defaults(fn=cmd_map)
    p = sub.add_parser("families", help="(private) seed sources -> simulator family templates")
    p.add_argument("--seed-tags", required=True); p.add_argument("--sources", nargs="+", required=True); p.add_argument("--out", required=True)
    p.add_argument("--max-assets", type=int, default=8); p.set_defaults(fn=cmd_families)
    p = sub.add_parser("parker", help="(private) Parker SSD parameter tables -> vendor maps")
    p.add_argument("--seed-tags", required=True); p.add_argument("--out", required=True); p.set_defaults(fn=cmd_parker)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
