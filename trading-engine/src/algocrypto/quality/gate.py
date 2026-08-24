"""Rulebook §9 weighted confidence engine (0–100)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from algocrypto.config import AppConfig
from algocrypto.models.events import Bias, CandidateSignal, FeatureSnapshot, MarketRegime

IST = ZoneInfo("Asia/Kolkata")

# Rulebook §9 component max weights
WEIGHTS = {
  "spot_vwap": 20,
  "option_vwap": 15,
  "market_regime": 15,
  "volume": 10,
  "oi": 10,
  "ema": 10,
  "delta": 8,
  "gamma": 5,
  "theta": 2,
  "vega_iv": 3,
  "spread": 2,
}


class QualityGate:
  """Institutional weighted confidence. Trade if >= min_confidence (Valid=75)."""

  def __init__(self, config: AppConfig) -> None:
    router = config.strategy.get("router", {})
    self.min_confidence = int(router.get("min_confidence", 75))
    self._config = config
    self._learner = None

  def set_learner(self, learner) -> None:
    self._learner = learner

  def score(
    self,
    signal: CandidateSignal,
    features: FeatureSnapshot,
    regime: MarketRegime,
    *,
    context: dict[str, Any] | None = None,
  ) -> tuple[int, list[str]]:
    ctx = context or {}
    extra = features.extra or {}
    chain = extra.get("chain") or {}
    logs: list[str] = []
    components: dict[str, float] = {k: 0.0 for k in WEIGHTS}

    # --- Spot VWAP (20) ---
    spot = features.nifty_spot
    vwap = features.session_vwap
    if spot is not None and vwap is not None:
      if signal.side == "CE" and spot > vwap:
        components["spot_vwap"] = 20
        logs.append("spot_vwap_bull=+20")
      elif signal.side == "PE" and spot < vwap:
        components["spot_vwap"] = 20
        logs.append("spot_vwap_bear=+20")
      elif (signal.side == "CE" and spot < vwap) or (
        signal.side == "PE" and spot > vwap
      ):
        components["spot_vwap"] = 0
        logs.append("spot_vwap_against=0")
      else:
        components["spot_vwap"] = 8
        logs.append("spot_vwap_neutral=+8")
    else:
      logs.append("spot_vwap_missing=0")

    # --- Option VWAP (15) ---
    opt_vwap = ctx.get("option_vwap") or extra.get("option_vwap")
    opt_ltp = ctx.get("ltp")
    if opt_vwap is not None and opt_ltp is not None:
      ov = Decimal(str(opt_vwap))
      ol = Decimal(str(opt_ltp))
      if ol >= ov:
        components["option_vwap"] = 15
        logs.append("option_above_vwap=+15")
      else:
        components["option_vwap"] = 4
        logs.append("option_below_vwap=+4")
    else:
      components["option_vwap"] = 7  # neutral when missing
      logs.append("option_vwap_unknown=+7")

    # --- Market regime (15) ---
    primary = regime.primary
    if signal.side == "CE" and primary in ("trending_up", "breakout", "low_volatility"):
      components["market_regime"] = 15
    elif signal.side == "PE" and primary in ("trending_down", "breakout", "low_volatility"):
      components["market_regime"] = 15
    elif primary == "sideways":
      components["market_regime"] = 2
    elif primary == "high_volatility":
      components["market_regime"] = 6
    elif primary in ("opening_range", "expiry_behaviour"):
      components["market_regime"] = 5
    else:
      components["market_regime"] = 8
    logs.append(f"regime_{primary}=+{components['market_regime']:.0f}")

    # --- Volume (10) ---
    vol = ctx.get("volume") or extra.get("option_volume")
    min_vol = int(self._config.validator.get("min_option_volume", 100000))
    if vol is not None:
      if int(vol) >= min_vol:
        components["volume"] = 10
        logs.append("volume_ok=+10")
      elif int(vol) >= min_vol // 2:
        components["volume"] = 6
        logs.append("volume_ok_half=+6")
      else:
        components["volume"] = 2
        logs.append("volume_thin=+2")
    else:
      components["volume"] = 5
      logs.append("volume_unknown=+5")

    # --- OI (10) ---
    oi = ctx.get("oi")
    if signal.side == "CE" and chain.get("oi_confirms_ce"):
      components["oi"] = 10
      logs.append("oi_confirms_ce=+10")
    elif signal.side == "PE" and chain.get("oi_confirms_pe"):
      components["oi"] = 10
      logs.append("oi_confirms_pe=+10")
    elif oi is not None and int(oi) >= int(
      self._config.validator.get("min_option_oi", 500000)
    ):
      components["oi"] = 7
      logs.append("oi_floor=+7")
    elif chain.get("long_build_up"):
      components["oi"] = 6
      logs.append("oi_long_build=+6")
    else:
      components["oi"] = 4
      logs.append("oi_neutral=+4")

    # --- EMA (10) ---
    e9, e21 = extra.get("ema9"), extra.get("ema21")
    if e9 is not None and e21 is not None and spot is not None:
      if signal.side == "CE" and e9 > e21 and float(spot) >= e21:
        components["ema"] = 10
        logs.append("ema_bull_align=+10")
      elif signal.side == "PE" and e9 < e21 and float(spot) <= e21:
        components["ema"] = 10
        logs.append("ema_bear_align=+10")
      else:
        components["ema"] = 3
        logs.append("ema_misalign=+3")
    else:
      components["ema"] = 5
      logs.append("ema_unknown=+5")

    # --- Delta (8) ---
    delta = ctx.get("delta")
    if delta is not None:
      ad = abs(float(delta))
      if 0.45 <= ad <= 0.65:
        components["delta"] = 8
        logs.append("delta_in_band=+8")
      elif 0.35 <= ad <= 0.75:
        components["delta"] = 5
        logs.append("delta_near=+5")
      else:
        components["delta"] = 2
        logs.append("delta_out=+2")
    else:
      components["delta"] = 4
      logs.append("delta_unknown=+4")

    # --- Gamma (5) ---
    gamma = ctx.get("gamma")
    if gamma is not None:
      g = float(gamma)
      if g >= 0.001:
        components["gamma"] = 5
        logs.append("gamma_strong=+5")
      elif g >= 0.0005:
        components["gamma"] = 3
        logs.append("gamma_ok=+3")
      else:
        components["gamma"] = 1
        logs.append("gamma_low=+1")
    else:
      components["gamma"] = 2
      logs.append("gamma_unknown=+2")

    # --- Theta (2) late-day penalty ---
    ts = features.ts
    theta_score = 2.0
    if ts is not None:
      local = ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)
      hm = local.hour * 60 + local.minute
      if hm >= 14 * 60 + 30:
        theta_score = 0.0
        logs.append("theta_late=0")
      elif hm >= 12 * 60:
        theta_score = 1.0
        logs.append("theta_midday=+1")
      else:
        logs.append("theta_ok=+2")
    components["theta"] = theta_score

    # --- Vega / IV (3) ---
    iv = ctx.get("iv")
    if iv is not None:
      ivf = float(iv)
      # IV as decimal (0.14) or percent (14)
      if ivf > 1.5:
        ivf = ivf / 100.0
      if ivf < 0.20:
        components["vega_iv"] = 3
        logs.append("iv_cheap=+3")
      elif ivf <= 0.60:
        components["vega_iv"] = 2
        logs.append("iv_normal=+2")
      elif ivf <= 0.80:
        components["vega_iv"] = 1
        logs.append("iv_elevated=+1")
      else:
        components["vega_iv"] = 0
        logs.append("iv_expensive=0")
    else:
      components["vega_iv"] = 1.5
      logs.append("iv_unknown=+1.5")

    # --- Spread (2) ---
    spread = ctx.get("spread_pct")
    max_sp = float(self._config.validator.get("max_spread_pct", 2.0))
    if spread is not None:
      sp = float(spread)
      if sp <= max_sp:
        components["spread"] = 2
        logs.append("spread_ok=+2")
      elif sp <= max_sp * 2:
        components["spread"] = 1
        logs.append("spread_wide=+1")
      else:
        components["spread"] = 0
        logs.append("spread_too_wide=0")
    else:
      components["spread"] = 1
      logs.append("spread_unknown=+1")

    raw = sum(components.values())
    conf = int(round(max(0.0, min(100.0, raw))))

    # Bias sanity
    if features.bias_5m == Bias.NEUTRAL and signal.setup_type not in (
      "mean_reversion",
      "liquidity_sweep",
      "reversal",
    ):
      conf = min(conf, 64)
      logs.append("neutral_bias_cap=64")

    # Analytics learning loop — demote sustained underperformers
    if self._learner is not None:
      mult = self._learner.priority_multiplier(signal.setup_type)
      if mult < 1.0:
        adj = int(round(conf * mult))
        logs.append(f"learner_mult={mult:.2f} conf {conf}->{adj}")
        conf = adj
      # Phase 7: soft demote on weak health (min sample already enforced in learner)
      try:
        snap = self._learner.snapshot()
        st = (snap.get("stats") or {}).get(signal.setup_type) or {}
        if st.get("health_label") == "weak":
          health_mult = float(
            (self._config.strategy.get("strategy_health") or {}).get(
              "demote_weak_multiplier", 0.85
            )
          )
          adj = int(round(conf * health_mult))
          logs.append(f"health_weak={st.get('health_score')} conf {conf}->{adj}")
          conf = adj
      except Exception:
        pass

    # Phase 5: soft IV regime × setup quality (never hard-reject alone)
    from algocrypto.option_data.vol_model import IVRegime, iv_setup_quality
    from algocrypto.strategy.families import strategy_family

    iv_reg_raw = ctx.get("iv_regime") or extra.get("iv_regime")
    try:
      iv_reg = IVRegime(iv_reg_raw) if iv_reg_raw else IVRegime.IV_UNKNOWN
    except ValueError:
      iv_reg = IVRegime.IV_UNKNOWN
    iv_adj, iv_why = iv_setup_quality(
      regime=iv_reg,
      setup_family=strategy_family(signal.setup_type),
      iv_rank=ctx.get("iv_rank") if ctx.get("iv_rank") is not None else extra.get("iv_rank"),
      cfg=self._config.strategy.get("vol_model") or {},
    )
    if iv_adj:
      conf = max(0, min(100, conf + iv_adj))
      logs.append(f"iv_setup_quality={iv_adj} ({iv_why})")

    # Persist component breakdown on signal metadata for §16 JSON mapping
    signal.scanner_metadata = {
      **signal.scanner_metadata,
      "confidence_components": {k: round(v, 2) for k, v in components.items()},
      "spot_vwap_score": components["spot_vwap"],
      "option_vwap_score": components["option_vwap"],
      "volume_score": components["volume"],
      "oi_score": components["oi"],
      "delta_score": components["delta"],
      "gamma_score": components["gamma"],
      "theta_score": components["theta"],
      "iv_score": components["vega_iv"],
      "spread_score": components["spread"],
      "iv_setup_adj": iv_adj,
      "iv_setup_reason": iv_why,
      "strategy_family": strategy_family(signal.setup_type),
    }

    logs.append(f"total={conf}")
    return conf, logs

  def passes(self, confidence: int) -> bool:
    return confidence >= self.min_confidence
