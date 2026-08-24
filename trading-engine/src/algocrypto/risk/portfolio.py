"""Portfolio exposure tracking (Gap-Fix Phase 4 / §20).

BTC and ETH same-direction options are treated as correlated, not independent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from algocrypto.symbols_util import underlying_from_tsym


@dataclass
class PortfolioSnapshot:
  premium_by_underlying: dict[str, Decimal] = field(default_factory=dict)
  premium_by_side: dict[str, Decimal] = field(default_factory=dict)
  # key "BTC:CE", "ETH:PE", ...
  premium_by_und_side: dict[str, Decimal] = field(default_factory=dict)
  combined_premium: Decimal = Decimal("0")
  directional_ce: Decimal = Decimal("0")
  directional_pe: Decimal = Decimal("0")
  net_delta: float = 0.0
  net_gamma: float = 0.0
  net_vega: float = 0.0
  net_theta: float = 0.0
  position_count: int = 0


@dataclass(frozen=True)
class PortfolioDecision:
  allow: bool
  reason: str | None = None
  details: dict[str, Any] = field(default_factory=dict)


def _norm_side(side: str | None) -> str:
  s = (side or "CE").upper()
  if s in ("C", "CALL", "CE"):
    return "CE"
  return "PE"


def _und(raw: str | None, tsym: str) -> str:
  u = (raw or underlying_from_tsym(tsym) or "OTHER").upper()
  if "BTC" in u:
    return "BTC"
  if "ETH" in u:
    return "ETH"
  return u


def build_portfolio_snapshot(positions: Iterable[Any]) -> PortfolioSnapshot:
  snap = PortfolioSnapshot()
  for pos in positions:
    tsym = getattr(pos, "tsym", "") or ""
    meta = getattr(pos, "signal_snapshot", None) or {}
    und_raw = meta.get("underlying") if isinstance(meta, dict) else None
    und = _und(und_raw, tsym)
    side = _norm_side(getattr(pos, "option_side", None))
    prem = Decimal(str(getattr(pos, "premium_deployed", 0) or 0))
    qty = int(getattr(pos, "quantity", 0) or 0)

    snap.premium_by_underlying[und] = (
      snap.premium_by_underlying.get(und, Decimal("0")) + prem
    )
    snap.premium_by_side[side] = snap.premium_by_side.get(side, Decimal("0")) + prem
    key = f"{und}:{side}"
    snap.premium_by_und_side[key] = snap.premium_by_und_side.get(key, Decimal("0")) + prem
    snap.combined_premium += prem
    if side == "CE":
      snap.directional_ce += prem
    else:
      snap.directional_pe += prem

    if isinstance(meta, dict):
      pick = meta.get("strike_pick") or {}
      for gname, attr in (
        ("delta", "net_delta"),
        ("gamma", "net_gamma"),
        ("vega", "net_vega"),
        ("theta", "net_theta"),
      ):
        raw = pick.get(gname) if isinstance(pick, dict) else None
        if raw is None:
          raw = meta.get(gname)
        if raw is not None:
          try:
            setattr(snap, attr, getattr(snap, attr) + float(raw) * qty)
          except (TypeError, ValueError):
            pass
    snap.position_count += 1
  return snap


def evaluate_portfolio_entry(
  risk_cfg: dict,
  *,
  snapshot: PortfolioSnapshot,
  equity: Decimal,
  new_underlying: str,
  new_side: str,
  new_premium: Decimal,
  new_delta: float | None = None,
) -> PortfolioDecision:
  if equity <= 0:
    return PortfolioDecision(False, "invalid_equity")

  und = _und(new_underlying, new_underlying)
  side = _norm_side(new_side)
  port = risk_cfg.get("portfolio") or risk_cfg
  corr = Decimal(str(port.get("btc_eth_correlation", 0.7)))

  max_und_pct = Decimal(str(port.get("max_underlying_exposure_pct", 35)))
  if max_und_pct > 0:
    und_prem = snapshot.premium_by_underlying.get(und, Decimal("0")) + new_premium
    pct = und_prem / equity * Decimal("100")
    if pct > max_und_pct:
      return PortfolioDecision(
        False,
        "portfolio_underlying_exposure_limit",
        {
          "underlying": und,
          "premium": str(und_prem),
          "pct": str(pct),
          "limit_pct": str(max_und_pct),
        },
      )

  max_comb_pct = Decimal(str(port.get("max_combined_exposure_pct", 50)))
  if max_comb_pct > 0:
    comb = snapshot.combined_premium + new_premium
    pct = comb / equity * Decimal("100")
    if pct > max_comb_pct:
      return PortfolioDecision(
        False,
        "portfolio_exposure_limit",
        {
          "combined_premium": str(comb),
          "pct": str(pct),
          "limit_pct": str(max_comb_pct),
        },
      )

  max_dir_pct = Decimal(str(port.get("max_directional_exposure_pct", 40)))
  if max_dir_pct > 0:
    existing = snapshot.directional_ce if side == "CE" else snapshot.directional_pe
    directional = existing + new_premium
    pct = directional / equity * Decimal("100")
    if pct > max_dir_pct:
      return PortfolioDecision(
        False,
        "portfolio_directional_exposure_limit",
        {
          "side": side,
          "premium": str(directional),
          "pct": str(pct),
          "limit_pct": str(max_dir_pct),
        },
      )

  # Correlated BTC↔ETH same option side
  max_corr_pct = Decimal(str(port.get("max_correlated_directional_pct", 45)))
  if max_corr_pct > 0 and und in ("BTC", "ETH") and corr > 0:
    other = "ETH" if und == "BTC" else "BTC"
    own_key = f"{und}:{side}"
    other_key = f"{other}:{side}"
    other_same = snapshot.premium_by_und_side.get(other_key, Decimal("0"))
    own_same = snapshot.premium_by_und_side.get(own_key, Decimal("0")) + new_premium
    if other_same > 0:
      correlated = own_same + other_same * corr
      pct = correlated / equity * Decimal("100")
      if pct > max_corr_pct:
        return PortfolioDecision(
          False,
          "portfolio_correlated_exposure_limit",
          {
            "underlying": und,
            "other": other,
            "side": side,
            "correlated_premium": str(correlated),
            "pct": str(pct),
            "limit_pct": str(max_corr_pct),
            "correlation": str(corr),
          },
        )

  max_abs_delta = float(port.get("max_abs_net_delta", 0) or 0)
  if max_abs_delta > 0 and new_delta is not None:
    projected = abs(snapshot.net_delta + float(new_delta))
    if projected > max_abs_delta:
      return PortfolioDecision(
        False,
        "portfolio_delta_limit",
        {"projected_abs_delta": projected, "limit": max_abs_delta},
      )

  return PortfolioDecision(
    True,
    None,
    {"combined_premium": str(snapshot.combined_premium + new_premium)},
  )
