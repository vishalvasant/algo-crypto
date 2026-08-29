"""Expected value gate (Gap-Fix Phase 6 / §11).

Uses historical strategy stats when available — never treats rule_score as P(win).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class EVResult:
  rule_score: int
  estimated_win_probability: float | None
  expected_win: float
  expected_loss: float
  fees: float
  expected_slippage: float
  expected_value: float
  eligible: bool
  reason: str | None
  detail: dict[str, Any]


def resolve_ev_engine_config(
  ev_cfg: dict | None,
  *,
  is_paper: bool,
  rule_score: int = 0,
) -> dict[str, Any]:
  """Merge live vs paper EV parameters; paper may bypass the gate when router passed."""
  cfg = dict(ev_cfg or {})
  paper_cfg = dict(cfg.get("paper") or {})
  min_ev = float(cfg.get("min_ev", 0.0))
  prior_pwin = float(cfg.get("prior_pwin", 0.45))
  skip_ev_block = False
  ev_mode = "live"

  if is_paper and bool(paper_cfg.get("enabled", True)):
    ev_mode = "paper"
    min_ev = float(paper_cfg.get("min_ev", min_ev))
    prior_pwin = float(paper_cfg.get("prior_pwin", prior_pwin))
    bypass_gte = paper_cfg.get("bypass_when_rule_score_gte")
    if bypass_gte is not None and rule_score >= int(bypass_gte):
      skip_ev_block = True

  return {
    "min_ev": min_ev,
    "prior_pwin": prior_pwin,
    "adverse_pct": float(cfg.get("adverse_pct", 0.12)),
    "reward_pct": float(cfg.get("reward_pct", 0.18)),
    "min_trades_for_pwin": int(cfg.get("min_trades_for_pwin", 8)),
    "assumed_fee_pct_of_premium": float(cfg.get("assumed_fee_pct_of_premium", 0.02)),
    "skip_ev_block": skip_ev_block,
    "ev_mode": ev_mode,
  }


def estimate_ev(
  *,
  rule_score: int,
  strategy: str,
  entry_premium_usd: Decimal | float,
  fees_usd: Decimal | float = 0,
  expected_slippage_usd: Decimal | float = 0,
  learner_snapshot: dict | None = None,
  adverse_pct: float = 0.12,
  reward_pct: float = 0.18,
  min_ev: float = 0.0,
  min_trades_for_pwin: int = 8,
  prior_pwin: float = 0.45,
) -> EVResult:
  """EV = Pwin*E[win] - Ploss*E[loss] - fees - slippage (per trade USD)."""
  prem = float(entry_premium_usd)
  fees = float(fees_usd)
  slip = float(expected_slippage_usd)
  e_win = prem * reward_pct
  e_loss = prem * adverse_pct

  pwin: float | None = None
  source = "prior"
  stats = ((learner_snapshot or {}).get("stats") or {}).get(strategy) or {}
  trades = int(stats.get("trades") or 0)
  if trades >= min_trades_for_pwin:
    wr = float(stats.get("win_rate") or 0)
    # Shrink toward prior
    pwin = (wr * trades + prior_pwin * min_trades_for_pwin) / (trades + min_trades_for_pwin)
    source = "learner_empirical"
  else:
    # Conservative: do not map rule_score→probability; use prior only
    pwin = prior_pwin
    source = "uninformative_prior"

  ploss = 1.0 - pwin
  ev = pwin * e_win - ploss * e_loss - fees - slip
  eligible = ev > float(min_ev)
  reason = None if eligible else "negative_expected_value"

  return EVResult(
    rule_score=rule_score,
    estimated_win_probability=pwin,
    expected_win=e_win,
    expected_loss=e_loss,
    fees=fees,
    expected_slippage=slip,
    expected_value=ev,
    eligible=eligible,
    reason=reason,
    detail={
      "pwin_source": source,
      "strategy": strategy,
      "trades_sample": trades,
      "premium_usd": prem,
      "min_ev": float(min_ev),
    },
  )
