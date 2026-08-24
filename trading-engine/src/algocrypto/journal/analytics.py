"""Performance analytics + strategy priority learning loop (§15 / Phase 7)."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from algocrypto.strategy.families import health_score_from_stats, strategy_family

logger = structlog.get_logger(__name__)


@dataclass
class StrategyStats:
  trades: int = 0
  wins: int = 0
  losses: int = 0
  pnl: float = 0.0
  win_pnl: float = 0.0
  loss_pnl: float = 0.0
  mfe_sum: float = 0.0
  mae_sum: float = 0.0
  recent: list[float] = field(default_factory=list)
  by_regime: dict[str, dict[str, float]] = field(default_factory=dict)
  by_underlying: dict[str, dict[str, float]] = field(default_factory=dict)
  by_expiry_bucket: dict[str, dict[str, float]] = field(default_factory=dict)
  flip_trades: int = 0
  flip_pnl: float = 0.0

  @property
  def win_rate(self) -> float:
    return (self.wins / self.trades) if self.trades else 0.0

  @property
  def expectancy(self) -> float:
    return (self.pnl / self.trades) if self.trades else 0.0

  @property
  def average_win(self) -> float:
    return (self.win_pnl / self.wins) if self.wins else 0.0

  @property
  def average_loss(self) -> float:
    return (self.loss_pnl / self.losses) if self.losses else 0.0

  @property
  def profit_factor(self) -> float:
    if self.loss_pnl >= 0 or abs(self.loss_pnl) < 1e-12:
      return float("inf") if self.win_pnl > 0 else 0.0
    return abs(self.win_pnl / self.loss_pnl)

  @property
  def avg_mfe(self) -> float:
    return self.mfe_sum / self.trades if self.trades else 0.0

  @property
  def avg_mae(self) -> float:
    return self.mae_sum / self.trades if self.trades else 0.0


def _bump(bucket: dict[str, dict[str, float]], key: str, pnl: float) -> None:
  row = bucket.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0})
  row["trades"] += 1
  row["pnl"] += pnl
  if pnl > 0:
    row["wins"] += 1


class StrategyLearner:
  """Reduce priority of strategies with sustained underperformance."""

  def __init__(
    self,
    path: Path | None = None,
    *,
    lookback: int = 20,
    demote_after_losses: int = 5,
    demote_multiplier: float = 0.75,
    promote_floor: float = 0.55,
    min_trades_health: int = 10,
  ) -> None:
    self._path = path
    self._lookback = lookback
    self._demote_after = demote_after_losses
    self._demote_mult = demote_multiplier
    self._promote_floor = promote_floor
    self._min_trades_health = min_trades_health
    self._stats: dict[str, StrategyStats] = {}
    self._multipliers: dict[str, float] = {}
    if path and path.exists():
      self._load(path)

  def record_trade(
    self,
    setup_type: str,
    pnl: Decimal | float,
    *,
    confidence: int | None = None,
    exit_reason: str | None = None,
    regime: str | None = None,
    underlying: str | None = None,
    expiry_bucket: str | None = None,
    mfe: float | None = None,
    mae: float | None = None,
  ) -> None:
    st = self._stats.setdefault(setup_type, StrategyStats())
    px = float(pnl)
    st.trades += 1
    st.pnl += px
    if px > 0:
      st.wins += 1
      st.win_pnl += px
    else:
      st.losses += 1
      st.loss_pnl += px
    if mfe is not None:
      st.mfe_sum += float(mfe)
    if mae is not None:
      st.mae_sum += float(mae)
    if setup_type == "trend_reversal_flip" or (exit_reason or "").startswith("flip"):
      st.flip_trades += 1
      st.flip_pnl += px
    if regime:
      _bump(st.by_regime, regime, px)
    if underlying:
      _bump(st.by_underlying, underlying.upper(), px)
    if expiry_bucket:
      _bump(st.by_expiry_bucket, expiry_bucket, px)
    st.recent.append(px)
    if len(st.recent) > self._lookback:
      st.recent = st.recent[-self._lookback :]
    self._recompute(setup_type)
    logger.info(
      "strategy_learner_recorded",
      setup=setup_type,
      family=strategy_family(setup_type),
      pnl=px,
      mult=self._multipliers.get(setup_type, 1.0),
      win_rate=round(st.win_rate, 3),
      confidence=confidence,
      exit_reason=exit_reason,
    )
    self._persist()

  def priority_multiplier(self, setup_type: str) -> float:
    return self._multipliers.get(setup_type, 1.0)

  def adjusted_confidence(self, setup_type: str, confidence: int) -> int:
    return int(round(confidence * self.priority_multiplier(setup_type)))

  def snapshot(self) -> dict[str, Any]:
    out_stats: dict[str, Any] = {}
    for k, v in self._stats.items():
      hs, label = health_score_from_stats(
        {
          "trades": v.trades,
          "win_rate": v.win_rate,
          "expectancy": v.expectancy,
          "average_win": v.average_win,
          "average_loss": v.average_loss,
          "profit_factor": v.profit_factor if v.profit_factor != float("inf") else 99,
        },
        min_trades=self._min_trades_health,
      )
      out_stats[k] = {
        "trades": v.trades,
        "wins": v.wins,
        "losses": v.losses,
        "pnl": round(v.pnl, 2),
        "win_rate": round(v.win_rate, 3),
        "expectancy": round(v.expectancy, 2),
        "average_win": round(v.average_win, 2),
        "average_loss": round(v.average_loss, 2),
        "profit_factor": round(v.profit_factor, 2) if v.profit_factor != float("inf") else None,
        "avg_mfe": round(v.avg_mfe, 4),
        "avg_mae": round(v.avg_mae, 4),
        "family": strategy_family(k),
        "health_score": hs,
        "health_label": label,
        "by_regime": v.by_regime,
        "by_underlying": v.by_underlying,
        "by_expiry_bucket": v.by_expiry_bucket,
        "flip_trades": v.flip_trades,
        "flip_pnl": round(v.flip_pnl, 2),
      }
    return {
      "multipliers": dict(self._multipliers),
      "stats": out_stats,
      "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }

  def _recompute(self, setup_type: str) -> None:
    st = self._stats[setup_type]
    recent = st.recent[-self._demote_after :]
    if len(recent) >= self._demote_after and all(x <= 0 for x in recent):
      self._multipliers[setup_type] = self._demote_mult
      return
    if st.trades >= 8 and st.win_rate < self._promote_floor and st.expectancy < 0:
      self._multipliers[setup_type] = min(
        self._multipliers.get(setup_type, 1.0), self._demote_mult
      )
      return
    if st.trades >= 5 and st.expectancy > 0 and st.win_rate >= 0.5:
      self._multipliers[setup_type] = 1.0

  def _persist(self) -> None:
    if not self._path:
      return
    try:
      self._path.parent.mkdir(parents=True, exist_ok=True)
      self._path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")
    except Exception:
      logger.exception("strategy_learner_persist_failed")

  def _load(self, path: Path) -> None:
    try:
      data = json.loads(path.read_text(encoding="utf-8"))
      self._multipliers = {k: float(v) for k, v in (data.get("multipliers") or {}).items()}
      for name, row in (data.get("stats") or {}).items():
        st = StrategyStats(
          trades=int(row.get("trades") or 0),
          wins=int(row.get("wins") or 0),
          losses=int(row.get("losses") or 0),
          pnl=float(row.get("pnl") or 0),
          win_pnl=float(row.get("average_win") or 0) * int(row.get("wins") or 0),
          loss_pnl=float(row.get("average_loss") or 0) * int(row.get("losses") or 0),
          by_regime=dict(row.get("by_regime") or {}),
          by_underlying=dict(row.get("by_underlying") or {}),
          by_expiry_bucket=dict(row.get("by_expiry_bucket") or {}),
          flip_trades=int(row.get("flip_trades") or 0),
          flip_pnl=float(row.get("flip_pnl") or 0),
        )
        self._stats[name] = st
    except Exception:
      logger.exception("strategy_learner_load_failed", path=str(path))
