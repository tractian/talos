# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Keeps the repo root importable (tests import each other as `tests.<module>`) whichever way pytest is launched."""
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
