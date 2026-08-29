from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from algocrypto.market_data.engine import MarketDataEngine
from algocrypto.models.events import CandleInterval


@dataclass
class ExitDecision:
  should_exit: bool
  reason: str | None = None


def _reversal_price(market_data: MarketDataEngine, cfg: dict) -> Decimal | None:
  """Price used for VWAP bias-flip — prefer 1m close over tick spot."""
  if cfg.get("trend_reversal_require_m1_close", True):
    m1 = market_data.candles(CandleInterval.M1)
    if m1:
      return m1[-1].close
  return market_data.spot_ltp


def _trend_reversal_triggered(
  *,
  option_side: str,
  price: Decimal,
  vwap: Decimal,
  buffer: Decimal,
) -> bool:
  if option_side == "CE" and price < (vwap - buffer):
    return True
  if option_side == "PE" and price > (vwap + buffer):
    return True
  return False


def _gross_pnl_usd(
  entry_price: Decimal,
  ltp: Decimal,
  lots: int,
  contract_size: Decimal,
) -> Decimal:
  if lots <= 0 or contract_size <= 0:
    return Decimal("0")
  return (ltp - entry_price) * Decimal(lots) * contract_size


def _estimate_round_trip_fees_usd(
  *,
  spot: Decimal,
  entry_price: Decimal,
  exit_ltp: Decimal,
  lots: int,
  contract_size: Decimal,
  fees_cfg: dict,
  entry_fee_usd: Decimal,
) -> Decimal:
  from algocrypto.fees import option_fee

  if spot <= 0 or lots <= 0 or contract_size <= 0:
    return entry_fee_usd
  entry_fee = entry_fee_usd
  if entry_fee <= 0:
    entry_fee = option_fee(
      spot=spot,
      premium=entry_price,
      lots=lots,
      contract_size=contract_size,
      fees_cfg=fees_cfg,
    ).total_fee_usd
  exit_fee = option_fee(
    spot=spot,
    premium=exit_ltp,
    lots=lots,
    contract_size=contract_size,
    fees_cfg=fees_cfg,
  ).total_fee_usd
  return entry_fee + exit_fee


def _trail_giveback_pct(cfg: dict, trail_key: str, trail_default: int, mfe_points: Decimal, entry_price: Decimal) -> Decimal:
  giveback_pct = Decimal(str(cfg.get(trail_key, trail_default))) / Decimal("100")
  large_mfe_pct = Decimal(str(cfg.get("large_mfe_trail_pct", 0))) / Decimal("100")
  if large_mfe_pct > 0 and entry_price > 0 and mfe_points >= entry_price * large_mfe_pct:
    tight = cfg.get("trail_giveback_pct_large_mfe")
    if tight is not None:
      giveback_pct = Decimal(str(tight)) / Decimal("100")
  return giveback_pct


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
  lots: int = 0,
  contract_size: Decimal | None = None,
  entry_fee_usd: Decimal = Decimal("0"),
  fees_cfg: dict | None = None,
) -> ExitDecision:
  if force_exit:
    return ExitDecision(True, "force_exit")

  now = now or datetime.now(tz=timezone.utc)
  if entry_ts.tzinfo is None and now.tzinfo is not None:
    entry_ts = entry_ts.replace(tzinfo=timezone.utc)
  min_hold = int(cfg.get("min_hold_seconds", 20))
  held_seconds = (now - entry_ts).total_seconds()
  if held_seconds < min_hold:
    return ExitDecision(False)

  max_hold = int(cfg.get("max_hold_minutes", 0))
  if max_hold > 0 and held_seconds > max_hold * 60:
    return ExitDecision(True, "time_stop")

  if entry_price <= 0 or current_ltp <= 0:
    return ExitDecision(False)

  high_vol = regime_primary == "high_volatility"
  adverse_key = (
    "high_vol_adverse_move_pct_from_entry" if high_vol else "adverse_move_pct_from_entry"
  )
  trail_key = "high_vol_trail_giveback_pct" if high_vol else "trail_giveback_pct"
  adverse_default = 10 if high_vol else 12
  trail_default = 30 if high_vol else 40

  adverse_pct = Decimal(str(cfg.get(adverse_key, adverse_default))) / Decimal("100")
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
  giveback_pct = _trail_giveback_pct(cfg, trail_key, trail_default, mfe_points, entry_price)
  size = contract_size if contract_size is not None and contract_size > 0 else Decimal("0")
  fee_aware = bool(cfg.get("trail_fee_aware", False)) and lots > 0 and size > 0
  min_gross_usd = Decimal(str(cfg.get("trail_min_gross_profit_usd", 0)))
  min_net_usd = Decimal(str(cfg.get("trail_min_net_profit_usd", 0)))
  fees = fees_cfg or {}

  trail_armed = mfe_points > 0 and mfe_points >= entry_price * min_profit_pct
  if trail_armed and fee_aware and min_gross_usd > 0:
    peak_gross = _gross_pnl_usd(entry_price, entry_price + mfe_points, lots, size)
    if peak_gross < min_gross_usd:
      trail_armed = False

  if trail_armed:
    trail_floor = entry_price + mfe_points * (Decimal("1") - giveback_pct)
    if current_ltp <= trail_floor:
      if fee_aware and min_net_usd > 0:
        spot = market_data.spot_ltp or Decimal("0")
        gross = _gross_pnl_usd(entry_price, current_ltp, lots, size)
        fees_total = _estimate_round_trip_fees_usd(
          spot=spot,
          entry_price=entry_price,
          exit_ltp=current_ltp,
          lots=lots,
          contract_size=size,
          fees_cfg=fees,
          entry_fee_usd=entry_fee_usd,
        )
        if gross - fees_total < min_net_usd:
          return ExitDecision(False)
      return ExitDecision(True, "momentum_trail")

  if cfg.get("bias_flip_exit", True):
    reversal_min_hold = int(cfg.get("trend_reversal_min_hold_seconds", min_hold))
    if held_seconds >= reversal_min_hold:
      defer_pct = Decimal(str(cfg.get("trend_reversal_defer_profit_pct", 0))) / Decimal("100")
      underwater_only = bool(cfg.get("trend_reversal_only_if_underwater", False))
      skip_reversal = False
      if underwater_only and current_ltp >= entry_price:
        skip_reversal = True
      elif defer_pct > 0 and current_ltp >= entry_price * (Decimal("1") + defer_pct):
        skip_reversal = True
      if not skip_reversal:
        spot = _reversal_price(market_data, cfg)
        vwap = market_data.session_vwap_value
        if spot is not None and vwap is not None:
          if atr is not None and atr > 0 and cfg.get("bias_flip_atr_multiplier") is not None:
            buffer = atr * Decimal(str(cfg.get("bias_flip_atr_multiplier", 0.35)))
          else:
            buffer = Decimal(str(cfg.get("bias_flip_buffer_points", 0)))
          if _trend_reversal_triggered(
            option_side=option_side,
            price=spot,
            vwap=vwap,
            buffer=buffer,
          ):
            return ExitDecision(True, "trend_reversal")

  return ExitDecision(False)
