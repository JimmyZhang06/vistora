"""Time boundary used to make scheduling and recovery deterministic."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current timezone-aware instant."""
