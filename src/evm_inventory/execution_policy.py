"""Execution timing policy; transaction sending is deliberately outside this module."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SequentialSchedule:
    delay_min_seconds: int = 30 * 60
    delay_max_seconds: int = 3 * 60 * 60

    def next_delay_seconds(self, rng: random.Random | None = None) -> int:
        if self.delay_min_seconds < 0 or self.delay_max_seconds < self.delay_min_seconds:
            raise ValueError("invalid sequential delay bounds")
        return (rng or random).randint(self.delay_min_seconds, self.delay_max_seconds)
