"""Trade thesis monitor (Gap-Fix Phase 6 / §21)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


@dataclass
class TradeThesis:
  direction: str  # CE | PE
  strategy: str
  regime: str | None
  bias: str | None
  spot_vs_vwap: str | None  # above | below | flat
  iv_regime: str | None
  structure: str | None
  entry_spot: Decimal | None
  entry_vwap: Decimal | None
  entry_ts: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
  meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ThesisAssessment:
  score: int  # 0–100
  degraded: bool
  reasons: tuple[str, ...]
  detail: dict[str, Any]


def build_thesis_from_signal(
  *,
  side: str,
  strategy: str,
  features_extra: dict | None = None,
  regime_primary: str | None = None,
  bias: str | None = None,
  spot: Decimal | None = None,
  vwap: Decimal | None = None,
  iv_regime: str | None = None,
) -> TradeThesis:
  extra = features_extra or {}
  rel = None
  if spot is not None and vwap is not None:
    if spot > vwap:
      rel = "above"
    elif spot < vwap:
      rel = "below"
    else:
      rel = "flat"
  return TradeThesis(
    direction=side,
    strategy=strategy,
    regime=regime_primary,
    bias=bias,
    spot_vs_vwap=rel,
    iv_regime=iv_regime,
    structure=str(extra.get("structure_5m")) if extra.get("structure_5m") else None,
    entry_spot=spot,
    entry_vwap=vwap,
    meta={"distance_to_vwap": extra.get("distance_to_vwap_points")},
  )


def assess_thesis(
  thesis: TradeThesis,
  *,
  spot: Decimal | None,
  vwap: Decimal | None,
  regime_primary: str | None,
  structure: str | None,
  iv_regime: str | None,
  cfg: dict | None = None,
) -> ThesisAssessment:
  """Score remaining thesis validity. Degrade when score < threshold."""
  cfg = cfg or {}
  score = 100
  reasons: list[str] = []

  # VWAP alignment for direction
  if spot is not None and vwap is not None:
    if thesis.direction == "CE" and spot < vwap:
      score -= 35
      reasons.append("ce_spot_below_vwap")
    elif thesis.direction == "PE" and spot > vwap:
      score -= 35
      reasons.append("pe_spot_above_vwap")
    elif thesis.spot_vs_vwap and (
      (thesis.direction == "CE" and spot > vwap)
      or (thesis.direction == "PE" and spot < vwap)
    ):
      reasons.append("vwap_aligned")

  # Regime flip against thesis
  if thesis.regime and regime_primary:
    adverse = (
      (thesis.direction == "CE" and regime_primary == "trending_down")
      or (thesis.direction == "PE" and regime_primary == "trending_up")
      or regime_primary == "sideways"
    )
    if adverse:
      score -= 25
      reasons.append(f"regime_adverse:{regime_primary}")

  # Structure breakdown
  if thesis.structure and structure:
    if thesis.direction == "CE" and structure == "lllh":
      score -= 20
      reasons.append("structure_turned_bearish")
    elif thesis.direction == "PE" and structure == "hhhl":
      score -= 20
      reasons.append("structure_turned_bullish")

  # IV collapse against long premium
  if iv_regime == "IV_CONTRACTING" and thesis.iv_regime == "IV_EXPANDING":
    score -= 15
    reasons.append("iv_expansion_faded")

  score = max(0, min(100, score))
  threshold = int(cfg.get("thesis_degrade_below", 40))
  degraded = score < threshold and bool(cfg.get("thesis_exit_enabled", True))
  return ThesisAssessment(
    score=score,
    degraded=degraded,
    reasons=tuple(reasons),
    detail={"threshold": threshold, "strategy": thesis.strategy},
  )
