"""Walk-forward / OOS helpers (Gap-Fix Phase 8)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence


@dataclass(frozen=True)
class DayResult:
  day: date
  trades: int
  wins: int
  pnl: float
  win_rate: float


@dataclass(frozen=True)
class WalkWindow:
  train_days: tuple[date, ...]
  test_days: tuple[date, ...]


def rolling_windows(
  days: Sequence[date],
  *,
  train_size: int = 3,
  test_size: int = 1,
) -> list[WalkWindow]:
  """Expanding/rolling train → next test_size days OOS."""
  ordered = sorted(days)
  out: list[WalkWindow] = []
  i = 0
  while i + train_size + test_size <= len(ordered):
    train = tuple(ordered[i : i + train_size])
    test = tuple(ordered[i + train_size : i + train_size + test_size])
    out.append(WalkWindow(train_days=train, test_days=test))
    i += test_size
  return out


def summarize_day_results(results: Iterable[DayResult]) -> dict[str, Any]:
  rows = list(results)
  trades = sum(r.trades for r in rows)
  wins = sum(r.wins for r in rows)
  pnl = sum(r.pnl for r in rows)
  return {
    "days": len(rows),
    "trades": trades,
    "wins": wins,
    "win_rate": (wins / trades) if trades else 0.0,
    "pnl": round(pnl, 2),
    "avg_pnl_per_day": round(pnl / len(rows), 2) if rows else 0.0,
  }


def walk_forward_report(
  day_map: dict[date, DayResult],
  *,
  train_size: int = 3,
  test_size: int = 1,
) -> dict[str, Any]:
  days = sorted(day_map.keys())
  windows = rolling_windows(days, train_size=train_size, test_size=test_size)
  folds: list[dict[str, Any]] = []
  for w in windows:
    is_rows = [day_map[d] for d in w.train_days if d in day_map]
    oos_rows = [day_map[d] for d in w.test_days if d in day_map]
    folds.append(
      {
        "train_days": [d.isoformat() for d in w.train_days],
        "test_days": [d.isoformat() for d in w.test_days],
        "in_sample": summarize_day_results(is_rows),
        "out_of_sample": summarize_day_results(oos_rows),
      }
    )
  oos_all = []
  for f in folds:
    # Reconstruct approximate OOS from fold summaries is lossy; recompute from days
    for d in f["test_days"]:
      dd = date.fromisoformat(d)
      if dd in day_map:
        oos_all.append(day_map[dd])
  return {
    "days_available": [d.isoformat() for d in days],
    "train_size": train_size,
    "test_size": test_size,
    "folds": folds,
    "aggregate_oos": summarize_day_results(oos_all),
  }
