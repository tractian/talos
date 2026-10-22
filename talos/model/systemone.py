# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""HTTP client for a System One compatible server: Kev's `kev.serve` (/v1/systemone), TypeSafe's native endpoint,
or Jev via OpenRouter (/api/alpha/decisions). Timeout raises JudgeTimeout; the gate falls back deterministically."""
from __future__ import annotations

import os

from .base import Answers, JudgeError, JudgeTimeout, normalize_answers


def _httpx():
    """httpx is an optional dependency (`pip install talos[http]`); only this judge needs it."""
    try:
        import httpx
    except ImportError as e:
        raise JudgeError("SystemOneJudge needs httpx: pip install 'talos[http]'") from e
    return httpx


class SystemOneJudge:
    name = "systemone"

    def __init__(self, url: str, model: str = "kev-latest", api_key: str | None = None, timeout_s: float = 0.5):
        """url: full endpoint URL, e.g. http://127.0.0.1:8009/v1/systemone or https://openrouter.ai/api/alpha/decisions"""
        self._httpx = _httpx()
        self.url = url
        self.model = model
        self.api_key = api_key or os.environ.get("TALOS_JUDGE_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        self.timeout_s = timeout_s
        self.client = self._httpx.Client(timeout=timeout_s)

    def judge(self, body: dict, deadline_s: float | None = None) -> Answers:
        payload = dict(body)
        payload.setdefault("model", self.model)
        payload["model"] = self.model
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            r = self.client.post(self.url, json=payload, headers=headers, timeout=deadline_s or self.timeout_s)
        except self._httpx.TimeoutException as e:
            raise JudgeTimeout(str(e)) from e
        except self._httpx.HTTPError as e:
            raise JudgeError(str(e)) from e
        if r.status_code != 200:
            raise JudgeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return normalize_answers(r.json(), body["questions"])
