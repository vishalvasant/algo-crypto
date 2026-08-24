"""Safety / data-freshness layer (Gap-Fix Phase 1.2).

Separate from strategy confidence. Blocks new entries when feeds are unsafe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class SafetyState(str, Enum):
  NORMAL = "NORMAL"
  DEGRADED = "DEGRADED"
  HALTED = "HALTED"
  EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


@dataclass
class SafetySnapshot:
  state: SafetyState = SafetyState.NORMAL
  reasons: list[str] = field(default_factory=list)
  allow_entries: bool = True
  allow_exits: bool = True
  details: dict[str, Any] = field(default_factory=dict)
  ts: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


class SafetyEngine:
  """Monitor feed / broker freshness and expose a hard safety state."""

  def __init__(self, config) -> None:
    safety = {}
    if hasattr(config, "runtime"):
      safety = (config.runtime or {}).get("safety") or {}
    elif isinstance(config, dict):
      safety = config.get("safety") or {}
    self._cfg = safety
    self._state = SafetyState.NORMAL
    self._last_reasons: list[str] = []

  def evaluate(
    self,
    *,
    last_spot_quote_ts: datetime | None = None,
    last_option_quote_ts: datetime | None = None,
    last_candle_ts: datetime | None = None,
    candles_stale: bool = False,
    broker_connected: bool = True,
    db_ok: bool = True,
    now: datetime | None = None,
  ) -> SafetySnapshot:
    now = now or datetime.now(tz=timezone.utc)
    reasons: list[str] = []
    details: dict[str, Any] = {}

    soft_sec = float(self._cfg.get("quote_stale_soft_seconds", 30))
    hard_sec = float(self._cfg.get("quote_stale_hard_seconds", 90))
    candle_hard = float(self._cfg.get("candle_stale_hard_seconds", 180))

    def _age(ts: datetime | None) -> float | None:
      if ts is None:
        return None
      if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
      return (now - ts).total_seconds()

    spot_age = _age(last_spot_quote_ts)
    opt_age = _age(last_option_quote_ts)
    candle_age = _age(last_candle_ts)
    details["spot_quote_age_sec"] = spot_age
    details["option_quote_age_sec"] = opt_age
    details["candle_age_sec"] = candle_age

    degraded = False
    halted = False

    if not db_ok:
      halted = True
      reasons.append("database_unavailable")
    if not broker_connected:
      halted = True
      reasons.append("broker_unavailable")
    if candles_stale:
      halted = True
      reasons.append("candles_stale")

    for label, age in (("spot", spot_age), ("option", opt_age)):
      if age is None:
        if bool(self._cfg.get(f"require_{label}_quotes", False)):
          degraded = True
          reasons.append(f"{label}_quote_missing")
        continue
      if age >= hard_sec:
        halted = True
        reasons.append(f"{label}_quote_stale_hard")
      elif age >= soft_sec:
        degraded = True
        reasons.append(f"{label}_quote_stale_soft")

    if candle_age is not None and candle_age >= candle_hard:
      halted = True
      reasons.append("candle_stale_hard")

    if halted:
      state = SafetyState.HALTED
      allow_entries = False
    elif degraded:
      state = SafetyState.DEGRADED
      # Soft stale: block new entries by default; exits still allowed.
      allow_entries = not bool(self._cfg.get("block_entries_when_degraded", True))
    else:
      state = SafetyState.NORMAL
      allow_entries = True

    if state != self._state:
      logger.warning(
        "safety_state_change",
        from_state=self._state.value,
        to_state=state.value,
        reasons=reasons,
      )
      self._state = state
      self._last_reasons = list(reasons)

    return SafetySnapshot(
      state=state,
      reasons=reasons,
      allow_entries=allow_entries,
      allow_exits=True,
      details=details,
      ts=now,
    )
