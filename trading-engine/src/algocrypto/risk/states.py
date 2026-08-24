"""Portfolio risk state machine (Gap-Fix Phase 1)."""
from __future__ import annotations

from enum import Enum


class RiskState(str, Enum):
  """Hard risk states — separate from strategy confidence."""

  NORMAL = "NORMAL"
  WARNING = "WARNING"
  HALTED = "HALTED"
  EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


# States that block new entries (flips included unless noted).
ENTRY_BLOCKING_STATES = frozenset(
  {
    RiskState.HALTED,
    RiskState.EMERGENCY_FLATTEN,
  }
)
