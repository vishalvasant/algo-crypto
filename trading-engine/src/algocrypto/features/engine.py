from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from algocrypto.config import AppConfig
from algocrypto.features.crypto_scaling import atr_threshold
from algocrypto.features.indicators import (
  aggregate_from_m5,
  cpr_levels,
  ema,
  opening_range,
)
from algocrypto.features.setups import detect_all_setups
from algocrypto.market_data.atr import approx_atr
from algocrypto.market_data.engine import MarketDataEngine
from algocrypto.market_data.vwap import session_vwap
from algocrypto.models.events import Bias, Candle, CandleInterval, FeatureSnapshot
from algocrypto.position.reversal_confirm import bias_with_dead_zone


class FeatureEngine:
  def __init__(self, config: AppConfig, market_data: MarketDataEngine) -> None:
    self._config = config
    self._market_data = market_data
    self._reclaim = config.strategy.get("vwap_reclaim", {})
    self._pullback = config.strategy.get("vwap_pullback", {})
    self._vwap_bias = config.strategy.get("vwap_bias", {})
    self._crypto_scale = config.strategy.get("crypto_scaling", {})
    # Prior day OHLC for CPR / PDH / PDL / gap (set by orchestrator/backtest).
    self.prior_high: Decimal | None = None
    self.prior_low: Decimal | None = None
    self.prior_close: Decimal | None = None
    self.chain_snapshot: dict = {}
    self.is_expiry_day: bool = False
    self.option_context: dict = {}

  def set_prior_day(
    self,
    high: Decimal | None,
    low: Decimal | None,
    close: Decimal | None,
  ) -> None:
    self.prior_high = high
    self.prior_low = low
    self.prior_close = close

  def set_chain_snapshot(self, snap: dict) -> None:
    self.chain_snapshot = snap or {}

  def set_option_context(self, ctx: dict) -> None:
    self.option_context = ctx or {}

  def compute(self) -> FeatureSnapshot:
    m1 = self._market_data.candles(CandleInterval.M1)
    m3 = self._market_data.candles(CandleInterval.M3)
    m5 = self._market_data.candles(CandleInterval.M5)
    vwap = session_vwap(m1)
    spot = self._market_data.spot_ltp
    if spot is None and m1:
      spot = m1[-1].close

    # Structural bias from last 1m close so setups/triggers (candle-based)
    # stay aligned with bias. ATR dead zone avoids BULLISH↔BEARISH flicker
    # when spot chatters around VWAP (Gap-Fix Phase 2).
    price_for_bias = m1[-1].close if m1 else spot
    bias = Bias.NEUTRAL
    atr = approx_atr(m1, int(self._vwap_bias.get("atr_lookback_bars", 14)))
    dead_mult = Decimal(str(self._vwap_bias.get("dead_zone_atr_multiplier", 0.25)))
    if vwap and price_for_bias is not None:
      bias = bias_with_dead_zone(
        price=price_for_bias,
        vwap=vwap,
        atr=atr,
        dead_zone_atr_mult=dead_mult,
      )

    lookback = int(self._reclaim.get("setup_lookback_bars", 5))
    use_atr = bool(self._crypto_scale.get("use_atr_relative", True))
    max_dist = atr_threshold(
      atr,
      points=self._reclaim.get("max_distance_to_vwap_points"),
      atr_mult=self._crypto_scale.get("max_distance_to_vwap_atr"),
      default_points=28,
    ) or Decimal("28")
    trigger_lb = int(self._reclaim.get("trigger_lookback_bars", 3))
    max_fresh = int(self._reclaim.get("max_fresh_trigger_bars", 1))
    min_bars_with = int(self._reclaim.get("min_bars_with_vwap_3m", 0))

    setup_3m = _detect_reclaim_recent(m3, vwap, lookback) if vwap else None
    trigger_1m = (
      _detect_reclaim_trigger(m1, vwap, trigger_lb, max_fresh_bars=max_fresh)
      if vwap
      else None
    )

    # Drop reclaim labels that disagree with structural bias (avoids
    # "PE setup visible but never bought" / reclaim_side_mismatch).
    if setup_3m == "vwap_reclaim_bull" and bias != Bias.BULLISH:
      setup_3m = None
    elif setup_3m == "vwap_reclaim_bear" and bias != Bias.BEARISH:
      setup_3m = None
    if trigger_1m == "vwap_reclaim_cross_up" and bias != Bias.BULLISH:
      trigger_1m = None
    elif trigger_1m == "vwap_reclaim_cross_down" and bias != Bias.BEARISH:
      trigger_1m = None

    # Crypto: fresh 1m VWAP cross + aligned bias is a reclaim setup.
    reclaim_cross_dist_atr: float | None = None
    if use_atr and trigger_1m and setup_3m is None and vwap is not None:
      if trigger_1m == "vwap_reclaim_cross_up" and bias == Bias.BULLISH:
        setup_3m = "vwap_reclaim_bull"
      elif trigger_1m == "vwap_reclaim_cross_down" and bias == Bias.BEARISH:
        setup_3m = "vwap_reclaim_bear"
    if trigger_1m and vwap is not None and m1 and atr is not None and float(atr) > 0:
      reclaim_cross_dist_atr = float(abs(m1[-1].close - vwap) / atr)

    # Distance gate for reclaim setup (proximity to VWAP)
    if setup_3m and spot is not None and vwap is not None:
      dist = abs(spot - vwap)
      reclaim_live = (
        trigger_1m
        and setup_3m in ("vwap_reclaim_bull", "vwap_reclaim_bear")
      )
      if dist > max_dist and not reclaim_live:
        setup_3m = None
      elif (
        min_bars_with > 0
        and not trigger_1m
        and _bars_with_vwap(m3, vwap, bias) < min_bars_with
      ):
        setup_3m = None

    pullback_setup = None
    pullback_trigger = None
    if vwap and spot is not None:
      pb_min_ext = atr_threshold(
        atr,
        points=self._pullback.get("min_extension_points"),
        atr_mult=self._crypto_scale.get("pullback_min_extension_atr"),
        default_points=5,
      ) or Decimal("5")
      pb_max_dist = atr_threshold(
        atr,
        points=self._pullback.get("max_distance_to_vwap_points"),
        atr_mult=self._crypto_scale.get("pullback_max_distance_atr"),
        default_points=30,
      ) or Decimal("30")
      pullback_setup = _detect_pullback(
        m3,
        vwap,
        bias,
        lookback=int(self._pullback.get("setup_lookback_bars", 8)),
        min_extension=pb_min_ext,
        max_distance=pb_max_dist,
      )
      pullback_trigger = _detect_pullback_trigger(
        m1,
        vwap,
        bias,
        lookback=int(self._pullback.get("trigger_lookback_bars", 3)),
        max_near_vwap=pb_max_dist,
      )
      if pullback_setup and not pullback_trigger:
        pullback_trigger = _detect_pullback_trigger(
          m1,
          vwap,
          bias,
          lookback=int(self._pullback.get("trigger_lookback_bars", 3)),
          max_near_vwap=pb_max_dist * Decimal("2"),
        )
      if pullback_setup and not pullback_trigger and len(m1) >= 2:
        prev_c, curr_c = m1[-2].close, m1[-1].close
        if bias == Bias.BULLISH and curr_c > prev_c:
          pullback_trigger = "vwap_pullback_bounce_up"
        elif bias == Bias.BEARISH and curr_c < prev_c:
          pullback_trigger = "vwap_pullback_bounce_down"

    trend_cfg = self._config.strategy.get("vwap_trend", {})
    trend_setup = None
    if vwap and spot is not None:
      tr_min = atr_threshold(
        atr,
        points=trend_cfg.get("min_distance_to_vwap_points"),
        atr_mult=self._crypto_scale.get("trend_min_distance_atr"),
        default_points=3,
      ) or Decimal("3")
      tr_max = atr_threshold(
        atr,
        points=trend_cfg.get("max_distance_to_vwap_points"),
        atr_mult=self._crypto_scale.get("trend_max_distance_atr"),
        default_points=50,
      ) or Decimal("50")
      trend_setup = _detect_trend_continuation(
        m3,
        m1,
        vwap,
        bias,
        min_bars=int(trend_cfg.get("min_bars_on_side", 3)),
        min_distance=tr_min,
        max_distance=tr_max,
        require_momentum=bool(trend_cfg.get("require_1m_momentum", True)),
      )

    structure = _structure_5m(m5, 6)
    distance = float(spot - vwap) if spot is not None and vwap is not None else None
    bars_against = _bars_against_vwap(m3, vwap, bias) if vwap else 0
    bars_with = _bars_with_vwap(m3, vwap, bias) if vwap else 0

    closes_1m = [c.close for c in m1]
    e9 = ema(closes_1m, 9)
    e21 = ema(closes_1m, 21)
    e50 = ema(closes_1m, 50)
    m15 = aggregate_from_m5(m5, 15)
    orb = opening_range(m1, minutes=15)
    cpr = None
    if self.prior_high and self.prior_low and self.prior_close:
      cpr = cpr_levels(self.prior_high, self.prior_low, self.prior_close)

    gap_points = None
    if self.prior_close is not None and m1:
      gap_points = float(m1[0].open - self.prior_close)

    opt_ctx = self.option_context
    ind = {
      "spot": spot,
      "ema9": e9,
      "ema21": e21,
      "ema50": e50,
      "or_high": orb.get("or_high") if orb else None,
      "or_low": orb.get("or_low") if orb else None,
      "cpr_pivot": cpr["pivot"] if cpr else None,
      "cpr_tc": cpr["tc"] if cpr else None,
      "cpr_bc": cpr["bc"] if cpr else None,
      "pdh": self.prior_high,
      "pdl": self.prior_low,
      "gap_points": gap_points,
      "abs_distance_to_vwap_points": abs(distance) if distance is not None else None,
      "structure_5m": structure,
      "option_delta": opt_ctx.get("delta"),
      "option_gamma": opt_ctx.get("gamma"),
      "option_iv": opt_ctx.get("iv"),
      "option_iv_change": opt_ctx.get("iv_change"),
      "option_vwap": opt_ctx.get("option_vwap"),
      "option_ltp": opt_ctx.get("ltp"),
      "spread_pct": opt_ctx.get("spread_pct"),
      "option_oi": opt_ctx.get("oi"),
      "option_volume": opt_ctx.get("volume"),
      "iv_regime": opt_ctx.get("iv_regime"),
      "iv_rank": opt_ctx.get("iv_rank"),
      "realized_vol": opt_ctx.get("realized_vol"),
      "expected_move": opt_ctx.get("expected_move"),
      "time_to_expiry_minutes": opt_ctx.get("time_to_expiry_minutes"),
      "expiry_bucket": opt_ctx.get("expiry_bucket"),
    }

    existing = {
      "setup_3m": setup_3m,
      "setup_vwap_pullback": pullback_setup,
      "setup_vwap_trend": trend_setup,
    }
    strategy_setups = detect_all_setups(
      bias=bias,
      spot=spot,
      vwap=vwap,
      m1=m1,
      m3=m3,
      m5=m5,
      m15=m15,
      existing=existing,
      ind=ind,
      chain=self.chain_snapshot,
      is_expiry=self.is_expiry_day,
    )

    # Why reclaim/pullback/trend may be inactive (for Decision Logs).
    skip_reasons: list[str] = []
    if not m1:
      skip_reasons.append("no_1m_candles")
    if not m3:
      skip_reasons.append("no_3m_candles")
    if vwap is None:
      skip_reasons.append("no_vwap")
    if spot is None:
      skip_reasons.append("no_spot")
    if setup_3m is None and vwap and spot is not None:
      skip_reasons.append("no_reclaim_setup")
    if trigger_1m is None and vwap:
      skip_reasons.append("no_reclaim_trigger")
    if pullback_setup is None:
      skip_reasons.append("no_pullback_setup")
    if pullback_trigger is None:
      skip_reasons.append("no_pullback_trigger")
    if trend_setup is None:
      skip_reasons.append("no_trend_setup")

    active = [k for k, v in strategy_setups.items() if v]
    if not active:
      skip_reasons.append("no_institutional_setups")

    extra = {
      "distance_to_vwap_points": distance,
      "abs_distance_to_vwap_points": abs(distance) if distance is not None else None,
      "structure_5m": structure,
      "setup_vwap_pullback": pullback_setup,
      "trigger_vwap_pullback": pullback_trigger,
      "setup_vwap_trend": trend_setup,
      "bars_against_vwap_3m": bars_against,
      "bars_with_vwap_3m": bars_with,
      "max_distance_to_vwap_points": float(max_dist),
      "max_distance_to_vwap_atr_mult": float(self._crypto_scale.get("max_distance_to_vwap_atr") or 0),
      "atr_1m": float(atr) if atr is not None else None,
      "reclaim_cross_distance_atr": reclaim_cross_dist_atr,
      "setup_lookback_bars": lookback,
      "require_5m_structure_reclaim": bool(self._reclaim.get("require_5m_structure", False)),
      "skip_reasons": skip_reasons,
      "candle_counts": {
        "m1": len(m1),
        "m3": len(m3),
        "m5": len(m5),
        "m15": len(m15),
      },
      "ema9": float(e9) if e9 is not None else None,
      "ema21": float(e21) if e21 is not None else None,
      "ema50": float(e50) if e50 is not None else None,
      "or_high": float(orb["or_high"]) if orb else None,
      "or_low": float(orb["or_low"]) if orb else None,
      "cpr": {k: float(v) for k, v in cpr.items()} if cpr else None,
      "pdh": float(self.prior_high) if self.prior_high is not None else None,
      "pdl": float(self.prior_low) if self.prior_low is not None else None,
      "gap_points": gap_points,
      "option_vwap": opt_ctx.get("option_vwap"),
      "option_iv": opt_ctx.get("iv"),
      "option_iv_change": opt_ctx.get("iv_change"),
      "iv_regime": opt_ctx.get("iv_regime"),
      "iv_rank": opt_ctx.get("iv_rank"),
      "iv_percentile": opt_ctx.get("iv_percentile"),
      "realized_vol": opt_ctx.get("realized_vol"),
      "iv_vs_rv": opt_ctx.get("iv_vs_rv"),
      "expected_move": opt_ctx.get("expected_move"),
      "time_to_expiry_minutes": opt_ctx.get("time_to_expiry_minutes"),
      "expiry_bucket": opt_ctx.get("expiry_bucket"),
      "vol_unavailable": opt_ctx.get("vol_unavailable"),
      "chain": self.chain_snapshot,
      "strategy_setups": strategy_setups,
      "active_setups": active,
    }

    return FeatureSnapshot(
      ts=datetime.now(tz=timezone.utc),
      nifty_spot=spot,
      session_vwap=vwap,
      bias_5m=bias,
      setup_3m=setup_3m,
      trigger_1m=trigger_1m,
      extra=extra,
    )


def _detect_reclaim(
  bars: list[Candle],
  vwap: Decimal,
  lookback: int,
) -> str | None:
  """N-bar VWAP reclaim: prior bar(s) on other side, current close reclaimed."""
  if len(bars) < 2:
    return None
  window = bars[-lookback:] if len(bars) >= lookback else bars
  if len(window) < 2:
    return None
  curr = window[-1].close
  priors = window[:-1]
  # Strict inequality first — avoid bull-wins-on-equal VWAP PE block.
  if curr > vwap and any(b.close < vwap for b in priors):
    return "vwap_reclaim_bull"
  if curr < vwap and any(b.close > vwap for b in priors):
    return "vwap_reclaim_bear"
  # Exact touch: infer direction from the most recent clear prior side.
  if curr == vwap:
    for b in reversed(priors):
      if b.close < vwap:
        return "vwap_reclaim_bull"
      if b.close > vwap:
        return "vwap_reclaim_bear"
  return None


def _detect_reclaim_recent(
  bars: list[Candle],
  vwap: Decimal,
  lookback: int,
) -> str | None:
  """Reclaim on current 3m bar or any bar in the recent window (crypto timing)."""
  found = _detect_reclaim(bars, vwap, lookback)
  if found:
    return found
  if len(bars) < 3:
    return None
  window = bars[-lookback:] if len(bars) >= lookback else bars
  for end in range(len(window) - 1, 0, -1):
    sub = window[: end + 1]
    label = _detect_reclaim(sub, vwap, len(sub))
    if label:
      return label
  return None


def _detect_reclaim_trigger(
  bars: list[Candle],
  vwap: Decimal,
  lookback: int,
  *,
  max_fresh_bars: int = 1,
) -> str | None:
  """1m reclaim cross; only the most recent cross within max_fresh_bars counts."""
  if len(bars) < 2:
    return None
  fresh = max(1, max_fresh_bars)
  start = max(1, len(bars) - fresh)
  found: str | None = None
  for i in range(start, len(bars)):
    prev = bars[i - 1].close
    curr = bars[i].close
    if prev < vwap <= curr:
      found = "vwap_reclaim_cross_up"
    elif prev > vwap >= curr:
      found = "vwap_reclaim_cross_down"
  return found


def _detect_pullback(
  bars: list[Candle],
  vwap: Decimal,
  bias: Bias,
  *,
  lookback: int,
  min_extension: Decimal,
  max_distance: Decimal,
) -> str | None:
  """Trend pullback toward VWAP after an extension, still on bias side."""
  if bias == Bias.NEUTRAL or len(bars) < 3:
    return None
  window = bars[-lookback:] if len(bars) >= lookback else bars
  curr = window[-1].close
  dist = abs(curr - vwap)
  if dist > max_distance:
    return None

  if bias == Bias.BULLISH:
    if curr < vwap:
      return None
    extended = any(b.close >= vwap + min_extension for b in window[:-1])
    pulling_back = len(window) >= 2 and abs(curr - vwap) < abs(window[-2].close - vwap)
    if extended and (dist <= max_distance or pulling_back):
      return "vwap_pullback_bull"
  elif bias == Bias.BEARISH:
    if curr > vwap:
      return None
    extended = any(b.close <= vwap - min_extension for b in window[:-1])
    pulling_back = len(window) >= 2 and abs(curr - vwap) < abs(window[-2].close - vwap)
    if extended and (dist <= max_distance or pulling_back):
      return "vwap_pullback_bear"
  return None


def _detect_pullback_trigger(
  bars: list[Candle],
  vwap: Decimal | None,
  bias: Bias,
  *,
  lookback: int,
  max_near_vwap: Decimal | None = None,
) -> str | None:
  """Bounce confirmation: latest 1m turns back in trend direction near VWAP."""
  if vwap is None or bias == Bias.NEUTRAL or len(bars) < 2:
    return None
  window = bars[-lookback:] if len(bars) >= lookback else bars
  if len(window) < 2:
    return None
  prev = window[-2].close
  curr = window[-1].close
  near = max_near_vwap if max_near_vwap is not None else Decimal("999999")
  if bias == Bias.BULLISH and curr > prev and curr >= vwap and abs(curr - vwap) <= near:
    return "vwap_pullback_bounce_up"
  if bias == Bias.BEARISH and curr < prev and curr <= vwap and abs(curr - vwap) <= near:
    return "vwap_pullback_bounce_down"
  return None


def _detect_trend_continuation(
  m3: list[Candle],
  m1: list[Candle],
  vwap: Decimal,
  bias: Bias,
  *,
  min_bars: int,
  min_distance: Decimal,
  max_distance: Decimal,
  require_momentum: bool,
) -> str | None:
  """Price already established on VWAP side + optional 1m momentum."""
  if bias == Bias.NEUTRAL or len(m3) < min_bars:
    return None
  window = m3[-min_bars:]
  curr = window[-1].close
  dist = abs(curr - vwap)
  if dist < min_distance or dist > max_distance:
    return None

  if bias == Bias.BULLISH:
    if any(b.close < vwap for b in window):
      return None
    if require_momentum:
      if len(m1) < 2 or m1[-1].close <= m1[-2].close:
        return None
    return "vwap_trend_bull"

  if bias == Bias.BEARISH:
    if any(b.close > vwap for b in window):
      return None
    if require_momentum:
      if len(m1) < 2 or m1[-1].close >= m1[-2].close:
        return None
    return "vwap_trend_bear"
  return None


def _structure_5m(m5: list[Candle], lookback: int) -> str:
  if len(m5) < 3:
    return "mixed"
  window = m5[-lookback:] if len(m5) >= lookback else m5
  highs = [c.high for c in window]
  lows = [c.low for c in window]
  hh = highs[-1] >= max(highs[:-1])
  hl = lows[-1] >= min(lows[:-1])
  ll = lows[-1] <= min(lows[:-1])
  lh = highs[-1] <= max(highs[:-1])
  bull = hh and hl
  bear = ll and lh
  # Tie → mixed so quality gate does not give CE a free +8 over PE.
  if bull and bear:
    return "mixed"
  if bull:
    return "hhhl"
  if bear:
    return "lllh"
  return "mixed"


def _bars_against_vwap(bars: list[Candle], vwap: Decimal, bias: Bias) -> int:
  if not bars or bias == Bias.NEUTRAL:
    return 0
  count = 0
  for b in reversed(bars[-8:]):
    if bias == Bias.BULLISH and b.close < vwap:
      count += 1
    elif bias == Bias.BEARISH and b.close > vwap:
      count += 1
    else:
      break
  return count


def _bars_with_vwap(bars: list[Candle], vwap: Decimal, bias: Bias) -> int:
  if not bars or bias == Bias.NEUTRAL:
    return 0
  count = 0
  for b in reversed(bars[-8:]):
    if bias == Bias.BULLISH and b.close > vwap:
      count += 1
    elif bias == Bias.BEARISH and b.close < vwap:
      count += 1
    else:
      break
  return count
