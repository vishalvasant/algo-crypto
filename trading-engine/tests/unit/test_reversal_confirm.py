"""Gap-Fix Phase 2: VWAP dead zone + reversal confirmation."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from algocrypto.models.events import Bias, Candle
from algocrypto.position.reversal_confirm import bias_with_dead_zone, confirm_reversal


def _candle(close: float, high: float | None = None, low: float | None = None, vol: int = 100):
  c = Decimal(str(close))
  h = Decimal(str(high if high is not None else close + 5))
  l = Decimal(str(low if low is not None else close - 5))
  return Candle(
    ts=datetime.now(tz=timezone.utc),
    interval="1m",
    open=c,
    high=h,
    low=l,
    close=c,
    volume=vol,
    instrument_token="BTCUSD",
  )


def test_dead_zone_keeps_neutral_near_vwap():
  atr = Decimal("100")
  bias = bias_with_dead_zone(
    price=Decimal("7710"),
    vwap=Decimal("7700"),
    atr=atr,
    dead_zone_atr_mult=Decimal("0.25"),  # zone = 25
  )
  assert bias == Bias.NEUTRAL


def test_dead_zone_bullish_when_clear():
  atr = Decimal("100")
  bias = bias_with_dead_zone(
    price=Decimal("7750"),
    vwap=Decimal("7700"),
    atr=atr,
    dead_zone_atr_mult=Decimal("0.25"),
  )
  assert bias == Bias.BULLISH


def test_small_vwap_cross_not_confirmed():
  """Tiny cross relative to ATR → blocked (no flip)."""
  vwap = Decimal("77000")
  spot = Decimal("77010")  # only 10 pts vs large ATR
  # Wide-range bars → ATR ~200+, required distance ~70
  m1 = [
    _candle(77000 + i, high=77200 + i, low=76800 + i) for i in range(14)
  ]
  m1.append(_candle(77010, high=77020, low=77000))
  conf = confirm_reversal(
    new_side="CE",
    spot=spot,
    vwap=vwap,
    m1=m1,
    m5=m1,
    cfg={
      "reversal_vwap_distance_atr_multiplier": 0.35,
      "reversal_atr_lookback_bars": 14,
      "reversal_require_candle_close": True,
      "reversal_require_momentum": True,
      "reversal_min_confirmation_score": 60,
      "bias_flip_buffer_points": 8,
    },
  )
  assert conf.confirmed is False
  assert "vwap_distance_insufficient" in conf.blockers



def test_confirmed_bearish_reversal():
  vwap = Decimal("77000")
  spot = Decimal("76500")  # well below
  # Declining closes
  m1 = [
    _candle(77200, 77250, 77150),
    _candle(77000, 77050, 76950),
    _candle(76800, 76850, 76700),
    _candle(76600, 76650, 76500),
    _candle(76500, 76550, 76400),
  ]
  # Pad ATR window with range
  for i in range(12):
    m1.insert(0, _candle(77000 + i * 10, 77100 + i * 10, 76900 + i * 10))
  conf = confirm_reversal(
    new_side="PE",
    spot=spot,
    vwap=vwap,
    m1=m1,
    m5=m1,
    cfg={
      "reversal_vwap_distance_atr_multiplier": 0.35,
      "reversal_atr_lookback_bars": 14,
      "reversal_require_candle_close": True,
      "reversal_require_momentum": True,
      "reversal_min_confirmation_score": 60,
    },
  )
  assert conf.confirmed is True
  assert conf.score >= 60
  assert not conf.blockers
