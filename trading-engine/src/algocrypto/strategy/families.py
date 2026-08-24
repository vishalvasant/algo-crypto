"""Strategy families + health scoring (Gap-Fix Phase 7)."""
from __future__ import annotations

from typing import Any

# Family membership for correlated-signal grouping
STRATEGY_FAMILIES: dict[str, str] = {
  "vwap_reclaim": "VWAP",
  "vwap_bounce": "VWAP",
  "vwap_pullback": "VWAP",
  "vwap_rejection": "VWAP",
  "vwap_trend": "VWAP",
  "ema_pullback": "Trend",
  "trend_continuation": "Trend",
  "trend_day": "Trend",
  "momentum_continuation": "Momentum",
  "delta_momentum": "Momentum",
  "opening_range_breakout": "Breakout",
  "cpr_breakout": "Breakout",
  "pdh_pdl_break": "Breakout",
  "gap_and_go": "Breakout",
  "oi_breakout": "Options Microstructure",
  "gamma_expansion": "Options Microstructure",
  "iv_expansion": "Options Microstructure",
  "reversal": "Reversal",
  "mean_reversion": "Reversal",
  "liquidity_sweep": "Levels",
  "expiry_scalping": "Expiry",
  "trend_reversal_flip": "Reversal",
}


def strategy_family(name: str) -> str:
  return STRATEGY_FAMILIES.get(name, "Other")


def health_score_from_stats(
  stats: dict[str, Any],
  *,
  min_trades: int = 10,
) -> tuple[float, str]:
  """0–100 health. Insufficient sample → neutral 50."""
  trades = int(stats.get("trades") or 0)
  if trades < min_trades:
    return 50.0, "insufficient_sample"
  wr = float(stats.get("win_rate") or 0)
  exp = float(stats.get("expectancy") or 0)
  avg_win = float(stats.get("average_win") or 0)
  avg_loss = abs(float(stats.get("average_loss") or 0))
  pf = float(stats.get("profit_factor") or 0)

  score = 50.0
  score += (wr - 0.45) * 80  # win rate around 45% baseline
  score += max(-20.0, min(20.0, exp * 2))
  if avg_loss > 0 and avg_win > 0:
    score += max(-10.0, min(10.0, (avg_win / avg_loss - 1) * 10))
  if pf > 0:
    score += max(-10.0, min(15.0, (pf - 1) * 10))
  score = max(0.0, min(100.0, score))
  label = "healthy" if score >= 60 else ("weak" if score < 40 else "neutral")
  return round(score, 1), label
