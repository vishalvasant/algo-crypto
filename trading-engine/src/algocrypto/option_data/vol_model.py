"""Options volatility / TTE model (Gap-Fix Phase 5).

Does not invent values — marks unavailable when data is missing.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from math import sqrt
from typing import Deque

from algocrypto.models.events import Candle


class IVRegime(str, Enum):
  IV_EXPANDING = "IV_EXPANDING"
  IV_CONTRACTING = "IV_CONTRACTING"
  IV_NEUTRAL = "IV_NEUTRAL"
  IV_UNKNOWN = "IV_UNKNOWN"


class ExpiryBucket(str, Enum):
  GT_12H = ">12h"
  H4_12 = "4-12h"
  H1_4 = "1-4h"
  M30_60 = "30m-1h"
  LT_30M = "<30m"
  UNKNOWN = "unknown"


@dataclass
class VolSnapshot:
  iv: float | None
  iv_change: float | None
  iv_rank: float | None  # 0–100 vs recent history, None if unavailable
  iv_percentile: float | None
  iv_regime: IVRegime
  realized_vol: float | None  # annualized from 1m returns
  iv_vs_rv: float | None  # iv - rv
  expected_move: float | None  # spot * iv * sqrt(t_years)
  time_to_expiry_minutes: float | None
  expiry_bucket: ExpiryBucket
  unavailable: tuple[str, ...] = ()


@dataclass
class IVHistory:
  """Ring buffer of recent ATM IVs for rank/percentile."""

  maxlen: int = 120
  _values: Deque[float] = field(default_factory=deque)

  def __post_init__(self) -> None:
    self._values = deque(maxlen=self.maxlen)

  def push(self, iv: float | None) -> None:
    if iv is not None and iv > 0:
      self._values.append(float(iv))

  def rank(self, iv: float | None) -> float | None:
    if iv is None or len(self._values) < 10:
      return None
    below = sum(1 for x in self._values if x <= iv)
    return 100.0 * below / len(self._values)

  def percentile(self, iv: float | None) -> float | None:
    return self.rank(iv)

  def recent_slope(self, lookback: int = 10) -> float | None:
    if len(self._values) < lookback:
      return None
    window = list(self._values)[-lookback:]
    return window[-1] - window[0]


def minutes_to_expiry(
  expiry: date | datetime | None,
  *,
  now: datetime | None = None,
) -> float | None:
  if expiry is None:
    return None
  now_utc = now or datetime.now(tz=timezone.utc)
  if now_utc.tzinfo is None:
    now_utc = now_utc.replace(tzinfo=timezone.utc)
  if isinstance(expiry, datetime):
    exp = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
  else:
    # Crypto daily: treat as 17:30 IST ≈ 12:00 UTC common Delta cut
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    exp = datetime(expiry.year, expiry.month, expiry.day, 17, 30, tzinfo=IST).astimezone(
      timezone.utc
    )
  secs = (exp - now_utc).total_seconds()
  return max(secs / 60.0, 0.0)


def expiry_bucket(minutes: float | None) -> ExpiryBucket:
  if minutes is None:
    return ExpiryBucket.UNKNOWN
  if minutes >= 12 * 60:
    return ExpiryBucket.GT_12H
  if minutes >= 4 * 60:
    return ExpiryBucket.H4_12
  if minutes >= 60:
    return ExpiryBucket.H1_4
  if minutes >= 30:
    return ExpiryBucket.M30_60
  return ExpiryBucket.LT_30M


def realized_vol_from_candles(
  candles: list[Candle],
  *,
  lookback: int = 60,
) -> float | None:
  """Annualized RV from 1m log returns. None if insufficient data."""
  if len(candles) < max(lookback, 5):
    return None
  window = candles[-lookback:]
  rets: list[float] = []
  for a, b in zip(window, window[1:]):
    c0, c1 = float(a.close), float(b.close)
    if c0 <= 0 or c1 <= 0:
      continue
    rets.append((c1 - c0) / c0)
  if len(rets) < 5:
    return None
  mean = sum(rets) / len(rets)
  var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
  # 1m bars → annualize: sqrt(525600) ≈ 725 (365*24*60)
  return sqrt(var) * sqrt(365.0 * 24.0 * 60.0)


def classify_iv_regime(
  *,
  iv_change: float | None,
  expand_threshold: float = 0.02,
  contract_threshold: float = -0.02,
) -> IVRegime:
  if iv_change is None:
    return IVRegime.IV_UNKNOWN
  if iv_change >= expand_threshold:
    return IVRegime.IV_EXPANDING
  if iv_change <= contract_threshold:
    return IVRegime.IV_CONTRACTING
  return IVRegime.IV_NEUTRAL


def expected_move(
  *,
  spot: float | None,
  iv: float | None,
  t_years: float | None,
) -> float | None:
  if spot is None or iv is None or t_years is None or spot <= 0 or iv <= 0 or t_years <= 0:
    return None
  return spot * iv * sqrt(t_years)


def build_vol_snapshot(
  *,
  iv: float | None,
  history: IVHistory | None,
  candles: list[Candle],
  spot: float | None,
  expiry: date | datetime | None,
  now: datetime | None = None,
  cfg: dict | None = None,
) -> VolSnapshot:
  cfg = cfg or {}
  unavailable: list[str] = []
  hist = history or IVHistory()
  prev_iv = list(hist._values)[-1] if hist._values else None
  iv_change = None
  if iv is not None and prev_iv is not None:
    iv_change = iv - prev_iv
  elif iv is None:
    unavailable.append("iv")

  slope = hist.recent_slope(int(cfg.get("iv_slope_lookback", 10)))
  if iv_change is None and slope is not None:
    iv_change = slope

  regime = classify_iv_regime(
    iv_change=iv_change,
    expand_threshold=float(cfg.get("iv_expand_threshold", 0.02)),
    contract_threshold=float(cfg.get("iv_contract_threshold", -0.02)),
  )

  iv_rank = hist.rank(iv)
  if iv_rank is None:
    unavailable.append("iv_rank")

  rv = realized_vol_from_candles(candles, lookback=int(cfg.get("rv_lookback_bars", 60)))
  if rv is None:
    unavailable.append("realized_vol")

  iv_vs_rv = (iv - rv) if (iv is not None and rv is not None) else None
  if iv_vs_rv is None:
    unavailable.append("iv_vs_rv")

  mins = minutes_to_expiry(expiry, now=now)
  bucket = expiry_bucket(mins)
  t_years = (mins / (365.0 * 24.0 * 60.0)) if mins is not None else None
  em = expected_move(spot=spot, iv=iv, t_years=t_years)
  if em is None:
    unavailable.append("expected_move")
  if mins is None:
    unavailable.append("time_to_expiry")

  # Push current IV after computing change
  hist.push(iv)

  return VolSnapshot(
    iv=iv,
    iv_change=iv_change,
    iv_rank=iv_rank,
    iv_percentile=iv_rank,
    iv_regime=regime,
    realized_vol=rv,
    iv_vs_rv=iv_vs_rv,
    expected_move=em,
    time_to_expiry_minutes=mins,
    expiry_bucket=bucket,
    unavailable=tuple(unavailable),
  )


def tte_entry_adjustments(bucket: ExpiryBucket, cfg: dict | None = None) -> dict:
  """Config multipliers for near-expiry (size, min confidence, hold)."""
  cfg = (cfg or {}).get("tte_adjustments") or cfg or {}
  defaults = {
    ExpiryBucket.GT_12H: {"size_mult": 1.0, "min_confidence_add": 0, "max_hold_mult": 1.0},
    ExpiryBucket.H4_12: {"size_mult": 1.0, "min_confidence_add": 0, "max_hold_mult": 1.0},
    ExpiryBucket.H1_4: {"size_mult": 0.75, "min_confidence_add": 5, "max_hold_mult": 0.7},
    ExpiryBucket.M30_60: {"size_mult": 0.5, "min_confidence_add": 10, "max_hold_mult": 0.4},
    ExpiryBucket.LT_30M: {"size_mult": 0.25, "min_confidence_add": 15, "max_hold_mult": 0.25},
    ExpiryBucket.UNKNOWN: {"size_mult": 1.0, "min_confidence_add": 0, "max_hold_mult": 1.0},
  }
  base = defaults.get(bucket, defaults[ExpiryBucket.UNKNOWN])
  override = cfg.get(bucket.value) or {}
  return {**base, **override}


def iv_setup_quality(
  *,
  regime: IVRegime,
  setup_family: str,
  iv_rank: float | None,
  cfg: dict | None = None,
) -> tuple[int, str]:
  """Soft score adjustment (-20..+15) for directional setups vs IV regime."""
  cfg = cfg or {}
  expensive = float(cfg.get("iv_expensive_rank", 80))
  if regime == IVRegime.IV_UNKNOWN:
    return 0, "iv_unknown"

  family = setup_family.lower()
  breakoutish = any(x in family for x in ("breakout", "gap", "momentum", "trend", "oi_"))
  if regime == IVRegime.IV_EXPANDING and breakoutish:
    return 12, "breakout_with_iv_expansion"
  if regime == IVRegime.IV_CONTRACTING and breakoutish:
    return -10, "breakout_with_iv_contraction"
  if iv_rank is not None and iv_rank >= expensive:
    return -8, "iv_expensive_long_option"
  if regime == IVRegime.IV_NEUTRAL:
    return 0, "iv_neutral"
  return 0, "iv_no_adjust"
