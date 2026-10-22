# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Trap registers / decoy nodes: addresses no legitimate agent has any reason to touch. Any touch is a
deterministic deny and a reputation hit. No model involved."""
from __future__ import annotations

from .schema import Request
from .world import World


class TrapSet:
    def __init__(self, tags: set[str] | None = None):
        self.tags: set[str] = set(tags or ())

    @classmethod
    def from_world(cls, world: World) -> TrapSet:
        return cls(world.traps())

    def add(self, tag: str):
        self.tags.add(tag)

    def is_trap(self, request: Request) -> bool:
        return request.target.is_trap or request.target.tag in self.tags
