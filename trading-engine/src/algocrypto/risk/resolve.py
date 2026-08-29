"""Merge live vs paper risk parameters."""
from __future__ import annotations

from copy import deepcopy


# Circuit-breaker keys set to 0 when paper.disable_circuit_breakers is true.
_CIRCUIT_KEYS = (
  "max_consecutive_losses",
  "daily_loss_limit_amount",
  "daily_loss_limit_pct",
  "max_daily_loss",
  "max_trades_per_day",
  "max_losing_trades_per_hour",
  "max_flips_per_day",
  "max_flips_per_hour",
)


def resolve_risk_config(risk_cfg: dict | None, *, is_paper: bool) -> dict:
  """Return effective risk config; paper block can disable halts and relax deploy caps."""
  base = deepcopy(risk_cfg or {})
  paper = dict(base.get("paper") or {})
  if not is_paper or not bool(paper.get("enabled", True)):
    return base

  merged = {**base}
  for key, value in paper.items():
    if key in ("enabled", "disable_circuit_breakers", "relax_deploy_caps"):
      continue
    merged[key] = value

  if bool(paper.get("disable_circuit_breakers", True)):
    merged["paper_disable_circuit_breakers"] = True
    for key in _CIRCUIT_KEYS:
      merged[key] = 0

  if bool(paper.get("relax_deploy_caps", True)):
    merged["max_premium_pct_of_available"] = paper.get(
      "max_premium_pct_of_available", 85
    )
    merged["max_premium_deployed_pct"] = paper.get(
      "max_premium_deployed_pct", 85
    )
    merged["max_deployed_pct_of_equity"] = paper.get(
      "max_deployed_pct_of_equity", 85
    )
    merged["max_exposure_pct"] = paper.get("max_exposure_pct", 85)

  return merged
