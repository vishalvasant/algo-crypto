"""ATR helper shared by bias dead-zone and reversal confirmation."""
from __future__ import annotations

from decimal import Decimal

from algocrypto.models.events import Candle


def approx_atr(candles: list[Candle], lookback: int = 14) -> Decimal | None:
  if not candles or lookback < 1:
    return None
  window = candles[-lookback:]
  if len(window) < 2:
    return None
  trs: list[Decimal] = []
  prev_close = window[0].close
  for c in window[1:]:
    tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
    trs.append(tr)
    prev_close = c.close
  if not trs:
    return None
  return sum(trs, Decimal("0")) / Decimal(str(len(trs)))
