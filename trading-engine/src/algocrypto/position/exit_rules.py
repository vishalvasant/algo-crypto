from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from algocrypto.market_data.engine import MarketDataEngine


@dataclass
class ExitDecision:
  should_exit: bool
  reason: str | None = None


def evaluate_momentum_exit(
  *,
  option_side: str,
  entry_price: Decimal,
  entry_ts: datetime,
  current_ltp: Decimal,
  mfe_points: Decimal,
  market_data: MarketDataEngine,
  cfg: dict,
  force_exit: bool,
  regime_primary: str | None = None,
  now: datetime | None = None,
  atr: Decimal | None = None,
) -> ExitDecision:
  if force_exit:
    return ExitDecision(True, "force_exit")

  now = now or datetime.now(tz=timezone.utc)
  if entry_ts.tzinfo is None and now.tzinfo is not None:
    entry_ts = entry_ts.replace(tzinfo=timezone.utc)
  min_hold = int(cfg.get("min_hold_seconds", 20))
  if (now - entry_ts).total_seconds() < min_hold:
    return ExitDecision(False)

  max_hold = int(cfg.get("max_hold_minutes", 0))
  if max_hold > 0 and (now - entry_ts).total_seconds() > max_hold * 60:
    return ExitDecision(True, "time_stop")

  if entry_price <= 0 or current_ltp <= 0:
    return ExitDecision(False)

  # Trend / bias reversal — prefer ATR-relative buffer; fall back to fixed points.
  if cfg.get("bias_flip_exit", True):
    spot = market_data.spot_ltp
    vwap = market_data.session_vwap_value
    if atr is not None and atr > 0 and cfg.get("bias_flip_atr_multiplier") is not None:
      buffer = atr * Decimal(str(cfg.get("bias_flip_atr_multiplier", 0.35)))
    else:
      buffer = Decimal(str(cfg.get("bias_flip_buffer_points", 0)))
    if spot is not None and vwap is not None:
      if option_side == "CE" and spot < (vwap - buffer):
        return ExitDecision(True, "trend_reversal")
      if option_side == "PE" and spot > (vwap + buffer):
        return ExitDecision(True, "trend_reversal")

  high_vol = regime_primary == "high_volatility"
  adverse_key = (
    "high_vol_adverse_move_pct_from_entry" if high_vol else "adverse_move_pct_from_entry"
  )
  trail_key = "high_vol_trail_giveback_pct" if high_vol else "trail_giveback_pct"
  adverse_default = 10 if high_vol else 12
  trail_default = 30 if high_vol else 40

  adverse_pct = Decimal(str(cfg.get(adverse_key, adverse_default))) / Decimal("100")
  # Phase 6: soft ATR scale — high spot ATR → slightly wider adverse room
  if (
    bool(cfg.get("dynamic_exits_enabled", True))
    and atr is not None
    and atr > 0
  ):
    spot = market_data.spot_ltp
    if spot is not None and spot > 0:
      atr_frac = float(atr / spot)
      cap = float(cfg.get("dynamic_adverse_atr_scale_cap", 0.35))
      scale = Decimal(str(1.0 + min(cap, atr_frac * 80.0)))
      adverse_pct = adverse_pct * scale

  if current_ltp <= entry_price * (Decimal("1") - adverse_pct):
    return ExitDecision(True, "adverse_momentum")

  min_profit_pct = Decimal(str(cfg.get("min_profit_before_trail_pct", 18))) / Decimal("100")
  giveback_pct = Decimal(str(cfg.get(trail_key, trail_default))) / Decimal("100")
  if mfe_points > 0 and mfe_points >= entry_price * min_profit_pct:
    trail_floor = entry_price + mfe_points * (Decimal("1") - giveback_pct)
    if current_ltp <= trail_floor:
      return ExitDecision(True, "momentum_trail")

  return ExitDecision(False)
