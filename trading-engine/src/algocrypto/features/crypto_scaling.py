"""ATR-relative thresholds for BTC/ETH (vs NSE index points)."""
from __future__ import annotations

from decimal import Decimal

from algocrypto.models.events import CandidateSignal, FeatureSnapshot, MarketRegime

_TREND_FOLLOW = frozenset({"vwap_trend", "trend_continuation"})
_PULLBACK = frozenset({"vwap_pullback"})


def atr_threshold(
    atr: Decimal | None,
    *,
    points: Decimal | float | int | None,
    atr_mult: Decimal | float | None,
    default_points: Decimal | float | int = 0,
) -> Decimal | None:
    """Resolve a distance gate: prefer ATR multiplier when ATR is known."""
    if atr is not None and atr > 0 and atr_mult is not None:
        return atr * Decimal(str(atr_mult))
    if points is not None:
        return Decimal(str(points))
    if default_points:
        return Decimal(str(default_points))
    return None


def crypto_entry_allowed(
  strategy_cfg: dict,
  features: FeatureSnapshot,
  regime: MarketRegime,
  signal: CandidateSignal,
) -> tuple[bool, str]:
  """Sweet-zone gate: block chop at VWAP; trend setups need extension, not cap it."""
  cfg = strategy_cfg.get("crypto_entry", {})
  if not cfg.get("enabled", False):
    return True, ""

  allowed_regimes = set(cfg.get("allowed_regimes") or [])
  if allowed_regimes and regime.primary not in allowed_regimes:
    return False, f"regime_{regime.primary}"

  extra = features.extra or {}
  dist = extra.get("distance_to_vwap_points")
  atr = extra.get("atr_1m")
  if dist is None or atr is None or float(atr) <= 0:
    return True, ""

  abs_dist = abs(float(dist))
  atr_f = float(atr)
  min_mult = float(cfg.get("min_vwap_distance_atr", 0))
  max_mult = float(cfg.get("max_vwap_distance_atr", 0))
  setup = signal.setup_type

  if setup in _PULLBACK:
    return True, ""

  if setup in _TREND_FOLLOW:
    if min_mult > 0 and abs_dist < atr_f * min_mult:
      return False, "too_close_to_vwap"
    return True, ""

  if min_mult > 0 and abs_dist < atr_f * min_mult:
    return False, "too_close_to_vwap"
  if max_mult > 0 and abs_dist > atr_f * max_mult:
    return False, "too_extended"
  return True, ""
