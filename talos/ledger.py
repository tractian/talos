# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Append-only JSONL ledger with a sha256 hash chain. One row per decision."""
from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

import orjson


class Ledger:
    def __init__(self, path: str | Path | None, fsync: bool = False):
        self.path = Path(path) if path else None
        self.fsync = fsync
        self.prev = "0" * 64
        self.n = 0
        self._fh = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size:
                for row in self.iter_rows():
                    self.prev = row["hash"]; self.n = row["n"] + 1
            self._fh = open(self.path, "ab")

    def append(self, row: dict) -> str:
        row = dict(row)
        row["n"] = self.n
        row["t"] = time.time()
        row["prev"] = self.prev
        body = orjson.dumps(row, option=orjson.OPT_SORT_KEYS)
        h = hashlib.sha256(body).hexdigest()
        row["hash"] = h
        if self._fh:
            self._fh.write(orjson.dumps(row) + b"\n")
            self._fh.flush()
            if self.fsync:
                os.fsync(self._fh.fileno())
        self.prev = h
        self.n += 1
        return h

    def iter_rows(self) -> Iterator[dict]:
        if not self.path or not self.path.exists():
            return iter(())
        return (orjson.loads(line) for line in self.path.read_bytes().splitlines() if line.strip())

    def verify(self) -> tuple[bool, int | None]:
        prev = "0" * 64
        for row in self.iter_rows():
            h = row.pop("hash")
            if row.get("prev") != prev or hashlib.sha256(orjson.dumps(row, option=orjson.OPT_SORT_KEYS)).hexdigest() != h:
                return False, row.get("n")
            prev = h
        return True, None

    def close(self):
        if self._fh:
            self._fh.close(); self._fh = None
