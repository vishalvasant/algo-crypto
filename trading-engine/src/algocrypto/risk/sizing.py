"""Risk-based + multi-cap lot sizing (Gap-Fix Phase 4 / §19).

final_lots = min(confidence, risk, capital, liquidity, max_lots)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from algocrypto.risk.engine import lots_for_confidence
from algocrypto.symbols_util import premium_usd


@dataclass
class SizeBreakdown:
  confidence_lots: int
  risk_lots: int
  capital_lots: int
  liquidity_lots: int | None
  max_lots: int
  final_lots: int
  binding_reason: str
  limits: dict[str, int] = field(default_factory=dict)
  notes: list[str] = field(default_factory=list)


def max_loss_budget_usd(risk_cfg: dict, *, equity: Decimal) -> Decimal:
  amount = Decimal(str(risk_cfg.get("max_loss_per_trade_amount", 0) or 0))
  if amount > 0:
    return amount
  pct = Decimal(str(risk_cfg.get("max_loss_per_trade_pct", 0) or 0))
  if pct > 0 and equity > 0:
    return equity * pct / Decimal("100")
  return Decimal("0")


def adverse_move_fraction(risk_cfg: dict, exit_cfg: dict | None = None) -> Decimal:
  if risk_cfg.get("expected_adverse_move_pct") is not None:
    return Decimal(str(risk_cfg["expected_adverse_move_pct"])) / Decimal("100")
  exit_cfg = exit_cfg or {}
  return Decimal(str(exit_cfg.get("adverse_move_pct_from_entry", 12))) / Decimal("100")


def risk_based_lots(
  risk_cfg: dict,
  *,
  equity: Decimal,
  entry_ltp: Decimal,
  contract_size: Decimal,
  exit_cfg: dict | None = None,
) -> int | None:
  """Return risk-capped lots, or None if risk sizing disabled."""
  budget = max_loss_budget_usd(risk_cfg, equity=equity)
  if budget <= 0 or entry_ltp <= 0 or contract_size <= 0:
    return None
  frac = adverse_move_fraction(risk_cfg, exit_cfg)
  if frac <= 0:
    return None
  loss_per_lot = entry_ltp * contract_size * frac
  if loss_per_lot <= 0:
    return None
  return max(0, int(budget / loss_per_lot))


def capital_fit_lots(
  risk_cfg: dict,
  *,
  entry_ltp: Decimal,
  contract_size: Decimal,
  available: Decimal,
  deployed: Decimal,
  equity: Decimal,
  start_from: int,
) -> int:
  max_pct = Decimal(
    str(
      risk_cfg.get(
        "max_premium_deployed_pct",
        risk_cfg.get("max_premium_pct_of_available", 40),
      )
    )
  )
  max_for_trade = available * max_pct / Decimal("100")
  deploy_pct = Decimal(
    str(
      risk_cfg.get(
        "max_exposure_pct",
        risk_cfg.get("max_deployed_pct_of_equity", 50),
      )
    )
  )
  room = equity * deploy_pct / Decimal("100") - deployed

  def _ok(n: int) -> bool:
    if n < 1 or contract_size <= 0 or entry_ltp <= 0:
      return False
    prem = premium_usd(price=entry_ltp, lots=n, size=contract_size)
    return prem <= available and prem <= max_for_trade and prem <= room

  n = max(0, int(start_from))
  while n >= 1 and not _ok(n):
    n -= 1
  return n


def compute_lot_size(
  risk_cfg: dict,
  *,
  confidence: int,
  entry_ltp: Decimal,
  contract_size: Decimal,
  available: Decimal,
  deployed: Decimal,
  equity: Decimal,
  exit_cfg: dict | None = None,
  liquidity_lots: int | None = None,
) -> SizeBreakdown:
  sizing = risk_cfg.get("confidence_lot_sizing") or {}
  max_lots = int(sizing.get("max_lots", risk_cfg.get("default_lots", 10)))
  conf_lots = lots_for_confidence(risk_cfg, confidence)
  r_lots = risk_based_lots(
    risk_cfg,
    equity=equity,
    entry_ltp=entry_ltp,
    contract_size=contract_size,
    exit_cfg=exit_cfg,
  )

  # Progressive min()
  final = min(conf_lots, max_lots)
  binding = "confidence_limit" if conf_lots <= max_lots else "max_lots"
  if r_lots is not None and r_lots < final:
    final = r_lots
    binding = "risk_based_limit"
  if liquidity_lots is not None and liquidity_lots < final:
    final = max(0, liquidity_lots)
    binding = "liquidity_limit"

  before_capital = final
  cap_lots = capital_fit_lots(
    risk_cfg,
    entry_ltp=entry_ltp,
    contract_size=contract_size,
    available=available,
    deployed=deployed,
    equity=equity,
    start_from=final,
  )
  if cap_lots < before_capital:
    final = cap_lots
    binding = "capital_limit"

  if final < 1:
    binding = "insufficient_capital"

  limits = {
    "confidence_limit": conf_lots,
    "max_lots": max_lots,
    "capital_limit": cap_lots,
  }
  if r_lots is not None:
    limits["risk_based_limit"] = r_lots
  if liquidity_lots is not None:
    limits["liquidity_limit"] = liquidity_lots

  notes: list[str] = []
  if r_lots is not None and r_lots < conf_lots:
    notes.append(f"risk_cap {r_lots} < confidence {conf_lots}")
  if liquidity_lots is not None and liquidity_lots < conf_lots:
    notes.append(f"liquidity_cap {liquidity_lots}")
  if cap_lots < before_capital:
    notes.append(f"capital_step_down to {cap_lots}")

  return SizeBreakdown(
    confidence_lots=conf_lots,
    risk_lots=r_lots if r_lots is not None else max_lots,
    capital_lots=cap_lots,
    liquidity_lots=liquidity_lots,
    max_lots=max_lots,
    final_lots=max(0, final),
    binding_reason=binding,
    limits=limits,
    notes=notes,
  )
