"""Hard circuit-breaker evaluation (Gap-Fix §3.1).

Pure functions — no DB. RiskEngine applies results to daily_risk_state.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from algocrypto.risk.states import ENTRY_BLOCKING_STATES, RiskState


@dataclass(frozen=True)
class CircuitSnapshot:
  starting_capital: Decimal
  realized_pnl: Decimal
  trade_count: int
  consecutive_losses: int
  open_position_count: int
  flip_count: int
  flips_last_hour: int
  losing_trades_last_hour: int
  kill_switch: bool
  entries_blocked: bool
  risk_state: RiskState = RiskState.NORMAL
  flips_disabled: bool = False


@dataclass(frozen=True)
class CircuitDecision:
  """Result of evaluating hard risk gates."""

  allow_entry: bool
  allow_flip: bool
  state: RiskState
  reason: str | None = None
  warning_reasons: tuple[str, ...] = ()


def _cfg_int(risk_cfg: dict, *keys: str, default: int = 0) -> int:
  for key in keys:
    if key in risk_cfg and risk_cfg[key] is not None:
      try:
        return int(risk_cfg[key])
      except (TypeError, ValueError):
        continue
  return default


def _cfg_decimal(risk_cfg: dict, *keys: str, default: Decimal = Decimal("0")) -> Decimal:
  for key in keys:
    if key in risk_cfg and risk_cfg[key] is not None:
      try:
        return Decimal(str(risk_cfg[key]))
      except Exception:
        continue
  return default


def daily_loss_limit_usd(risk_cfg: dict, starting_capital: Decimal) -> Decimal:
  """Effective daily loss limit in USD (0 = disabled).

  Prefers explicit amount; else pct of starting capital; else legacy max_daily_loss.
  """
  amount = _cfg_decimal(risk_cfg, "daily_loss_limit_amount", "max_daily_loss")
  if amount > 0:
    return amount
  pct = _cfg_decimal(risk_cfg, "daily_loss_limit_pct")
  if pct > 0 and starting_capital > 0:
    return starting_capital * pct / Decimal("100")
  return Decimal("0")


def evaluate_circuit_breakers(
  risk_cfg: dict,
  snap: CircuitSnapshot,
  *,
  is_flip: bool = False,
) -> CircuitDecision:
  """Hard gates for new entries / flips. Does not reduce confidence — blocks."""
  warnings: list[str] = []

  if snap.kill_switch:
    return CircuitDecision(
      False, False, RiskState.EMERGENCY_FLATTEN, "kill_switch"
    )

  # Preserve explicit emergency / halted until cleared.
  if snap.risk_state == RiskState.EMERGENCY_FLATTEN:
    return CircuitDecision(
      False, False, RiskState.EMERGENCY_FLATTEN, "emergency_flatten"
    )
  if snap.risk_state == RiskState.HALTED or snap.entries_blocked:
    reason = snap.risk_state.name.lower() if snap.risk_state in ENTRY_BLOCKING_STATES else (
      "entries_blocked"
    )
    # Still allow reading flip permission separately when only flips_disabled.
    if snap.entries_blocked and snap.risk_state == RiskState.NORMAL:
      reason = "entries_blocked"
    if snap.risk_state == RiskState.HALTED or snap.entries_blocked:
      allow_flip = False
      return CircuitDecision(False, allow_flip, RiskState.HALTED, reason)

  loss_limit = daily_loss_limit_usd(risk_cfg, snap.starting_capital)
  if loss_limit > 0 and snap.realized_pnl <= -loss_limit:
    return CircuitDecision(
      False, False, RiskState.HALTED, "daily_loss_limit"
    )

  max_trades = _cfg_int(risk_cfg, "max_trades_per_day")
  if max_trades > 0 and snap.trade_count >= max_trades:
    return CircuitDecision(
      False, False, RiskState.HALTED, "max_trades_per_day"
    )

  max_consec = _cfg_int(risk_cfg, "max_consecutive_losses")
  if max_consec > 0 and snap.consecutive_losses >= max_consec:
    return CircuitDecision(
      False, False, RiskState.HALTED, "max_consecutive_losses"
    )

  max_losing_hour = _cfg_int(risk_cfg, "max_losing_trades_per_hour")
  if max_losing_hour > 0 and snap.losing_trades_last_hour >= max_losing_hour:
    return CircuitDecision(
      False, False, RiskState.HALTED, "max_losing_trades_per_hour"
    )

  max_pos = _cfg_int(
    risk_cfg, "max_positions", "max_concurrent_positions"
  )
  if max_pos > 0 and snap.open_position_count >= max_pos:
    return CircuitDecision(
      False, False, RiskState.WARNING, "max_positions"
    )

  # Soft warning: approaching daily loss (80% of limit).
  if loss_limit > 0 and snap.realized_pnl < 0:
    used = -snap.realized_pnl / loss_limit
    if used >= Decimal("0.8"):
      warnings.append("approaching_daily_loss_limit")

  max_flips_day = _cfg_int(risk_cfg, "max_flips_per_day")
  max_flips_hour = _cfg_int(risk_cfg, "max_flips_per_hour")
  flips_blocked = bool(snap.flips_disabled)
  flip_reason: str | None = None
  if max_flips_day > 0 and snap.flip_count >= max_flips_day:
    flips_blocked = True
    flip_reason = "max_flips_per_day"
  if max_flips_hour > 0 and snap.flips_last_hour >= max_flips_hour:
    flips_blocked = True
    flip_reason = "max_flips_per_hour"

  state = RiskState.WARNING if warnings else RiskState.NORMAL
  if is_flip and flips_blocked:
    return CircuitDecision(
      False, False, state, flip_reason or "flips_disabled", tuple(warnings)
    )

  return CircuitDecision(
    True,
    not flips_blocked,
    state,
    None,
    tuple(warnings),
  )


def post_trade_halt_reason(
  risk_cfg: dict,
  *,
  starting_capital: Decimal,
  realized_pnl_after: Decimal,
  trade_count_after: int,
  consecutive_losses_after: int,
  losing_trades_last_hour: int,
) -> str | None:
  """After a close, decide if we must HALT new entries."""
  loss_limit = daily_loss_limit_usd(risk_cfg, starting_capital)
  if loss_limit > 0 and realized_pnl_after <= -loss_limit:
    return "daily_loss_limit"
  max_trades = _cfg_int(risk_cfg, "max_trades_per_day")
  if max_trades > 0 and trade_count_after >= max_trades:
    return "max_trades_per_day"
  max_consec = _cfg_int(risk_cfg, "max_consecutive_losses")
  if max_consec > 0 and consecutive_losses_after >= max_consec:
    return "max_consecutive_losses"
  max_losing_hour = _cfg_int(risk_cfg, "max_losing_trades_per_hour")
  if max_losing_hour > 0 and losing_trades_last_hour >= max_losing_hour:
    return "max_losing_trades_per_hour"
  return None
