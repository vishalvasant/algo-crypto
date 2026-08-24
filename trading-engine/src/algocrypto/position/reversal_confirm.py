"""Reversal confirmation for CE↔PE flips (Gap-Fix Phase 2 / §5–8).

A trend_reversal exit does NOT guarantee an opposite entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from algocrypto.market_data.atr import approx_atr
from algocrypto.models.events import Bias, Candle, CandleInterval


@dataclass(frozen=True)
class ReversalConfirmation:
  confirmed: bool
  score: int
  reasons: tuple[str, ...]
  blockers: tuple[str, ...]
  atr: Decimal | None
  distance_to_vwap: Decimal | None
  required_distance: Decimal | None


def confirm_reversal(
  *,
  new_side: str,
  spot: Decimal | None,
  vwap: Decimal | None,
  m1: list[Candle],
  m5: list[Candle],
  cfg: dict,
) -> ReversalConfirmation:
  """Score a proposed flip. confirmed only if score >= min and no hard blockers."""
  reasons: list[str] = []
  blockers: list[str] = []
  score = 0

  if new_side not in ("CE", "PE"):
    return ReversalConfirmation(
      False, 0, (), ("invalid_side",), None, None, None
    )
  if spot is None or vwap is None:
    return ReversalConfirmation(
      False, 0, (), ("missing_spot_or_vwap",), None, None, None
    )

  lookback = int(cfg.get("reversal_atr_lookback_bars", 14))
  atr = approx_atr(m1, lookback)
  dist = abs(spot - vwap)
  atr_mult = Decimal(str(cfg.get("reversal_vwap_distance_atr_multiplier", 0.35)))
  required = (atr * atr_mult) if atr is not None else None

  # --- VWAP distance (hard if ATR known) ---
  if required is not None:
    if dist >= required:
      score += 30
      reasons.append(f"vwap_distance_ok dist={dist:.2f} >= {required:.2f}")
    else:
      blockers.append("vwap_distance_insufficient")
  else:
    # Fallback: fixed buffer points
    buf = Decimal(str(cfg.get("bias_flip_buffer_points", 8)))
    if dist >= buf:
      score += 20
      reasons.append("vwap_distance_buffer_ok")
    else:
      blockers.append("vwap_distance_insufficient")

  # --- Side alignment with spot vs VWAP ---
  if new_side == "CE" and spot > vwap:
    score += 15
    reasons.append("spot_above_vwap_for_ce")
  elif new_side == "PE" and spot < vwap:
    score += 15
    reasons.append("spot_below_vwap_for_pe")
  else:
    blockers.append("side_not_aligned_with_vwap")

  # --- Candle close confirmation ---
  if bool(cfg.get("reversal_require_candle_close", True)):
    if m1:
      close = m1[-1].close
      if new_side == "CE" and close > vwap:
        score += 20
        reasons.append("m1_close_above_vwap")
      elif new_side == "PE" and close < vwap:
        score += 20
        reasons.append("m1_close_below_vwap")
      else:
        blockers.append("candle_close_not_beyond_vwap")
    else:
      blockers.append("no_m1_candles")
  else:
    score += 10
    reasons.append("candle_close_not_required")

  # --- Momentum (last few 1m closes) ---
  if bool(cfg.get("reversal_require_momentum", True)):
    if len(m1) >= 3:
      c0, c1, c2 = m1[-3].close, m1[-2].close, m1[-1].close
      if new_side == "CE" and c2 > c1 >= c0:
        score += 15
        reasons.append("bullish_m1_momentum")
      elif new_side == "PE" and c2 < c1 <= c0:
        score += 15
        reasons.append("bearish_m1_momentum")
      elif new_side == "CE" and c2 > c0:
        score += 8
        reasons.append("weak_bullish_momentum")
      elif new_side == "PE" and c2 < c0:
        score += 8
        reasons.append("weak_bearish_momentum")
      else:
        blockers.append("momentum_not_aligned")
    else:
      blockers.append("insufficient_m1_for_momentum")
  else:
    score += 8
    reasons.append("momentum_not_required")

  # --- Structure on 5m ---
  if bool(cfg.get("reversal_require_structure", False)) and len(m5) >= 4:
    highs = [c.high for c in m5[-4:]]
    lows = [c.low for c in m5[-4:]]
    if new_side == "CE" and highs[-1] >= max(highs[:-1]) and lows[-1] >= min(lows[:-1]):
      score += 10
      reasons.append("bullish_5m_structure")
    elif new_side == "PE" and lows[-1] <= min(lows[:-1]) and highs[-1] <= max(highs[:-1]):
      score += 10
      reasons.append("bearish_5m_structure")
    else:
      blockers.append("structure_not_confirmed")

  # --- Volume (optional soft) ---
  if bool(cfg.get("reversal_require_volume", False)) and len(m1) >= 5:
    vols = [int(c.volume or 0) for c in m1[-5:]]
    avg = sum(vols[:-1]) / max(len(vols) - 1, 1)
    if vols[-1] >= avg * float(cfg.get("reversal_volume_mult", 1.1)):
      score += 10
      reasons.append("volume_confirmation")
    else:
      blockers.append("volume_not_confirmed")

  min_score = int(cfg.get("reversal_min_confirmation_score", 60))
  confirmed = score >= min_score and not blockers

  return ReversalConfirmation(
    confirmed=confirmed,
    score=min(100, score),
    reasons=tuple(reasons),
    blockers=tuple(blockers),
    atr=atr,
    distance_to_vwap=dist,
    required_distance=required,
  )


def bias_with_dead_zone(
  *,
  price: Decimal,
  vwap: Decimal,
  atr: Decimal | None,
  dead_zone_atr_mult: Decimal,
) -> Bias:
  """Classify bias; stay NEUTRAL inside ATR-relative VWAP dead zone."""
  if atr is not None and atr > 0 and dead_zone_atr_mult > 0:
    zone = atr * dead_zone_atr_mult
    if abs(price - vwap) < zone:
      return Bias.NEUTRAL
  if price > vwap:
    return Bias.BULLISH
  if price < vwap:
    return Bias.BEARISH
  return Bias.NEUTRAL
