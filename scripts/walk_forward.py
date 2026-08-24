"""Multi-day walk-forward harness over day snaps / day_backtest.

Usage (from repo root):
  CONFIG_DIR=./config PYTHONPATH=trading-engine/src \\
    trading-engine/.venv/bin/python scripts/walk_forward.py --from-snap \\
    --train-days 3 --test-days 1

Requires day snaps under reports/snaps/YYYY-MM-DD (see day_backtest --snap-only).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("STRUCTLOG_LEVEL", "WARNING")

from algocrypto.backtest.walk_forward import DayResult, walk_forward_report


def _discover_snap_days() -> list[date]:
  root = ROOT / "reports" / "snaps"
  if not root.exists():
    return []
  out: list[date] = []
  for p in sorted(root.iterdir()):
    if p.is_dir():
      try:
        out.append(date.fromisoformat(p.name))
      except ValueError:
        continue
  return out


async def _run_one(day: date, *, from_snap: bool) -> DayResult | None:
  import importlib.util

  path = ROOT / "scripts" / "day_backtest.py"
  spec = importlib.util.spec_from_file_location("day_backtest", path)
  assert spec and spec.loader
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)

  stats = await mod.run_backtest(day, from_snap=from_snap, return_stats=True)
  if stats is None:
    return None
  wins = sum(1 for t in stats.closed if t.pnl > 0)
  pnl = float(sum((t.pnl for t in stats.closed), Decimal("0")))
  n = len(stats.closed)
  return DayResult(
    day=day,
    trades=n,
    wins=wins,
    pnl=pnl,
    win_rate=(wins / n) if n else 0.0,
  )


async def main() -> None:
  import argparse

  parser = argparse.ArgumentParser(description="Walk-forward over day snaps")
  parser.add_argument("--from-snap", action="store_true", default=True)
  parser.add_argument("--train-days", type=int, default=3)
  parser.add_argument("--test-days", type=int, default=1)
  parser.add_argument(
    "--dates",
    nargs="*",
    help="Optional explicit YYYY-MM-DD list (default: all snaps)",
  )
  parser.add_argument(
    "--report",
    default=str(ROOT / "reports" / "walk_forward_report.json"),
  )
  args = parser.parse_args()

  if args.dates:
    days = [date.fromisoformat(d) for d in args.dates]
  else:
    days = _discover_snap_days()

  if not days:
    print("No snap days found under reports/snaps/. Create with:")
    print("  python scripts/day_backtest.py --date YYYY-MM-DD --snap-only")
    # Still emit empty structural report for CI / unit path
    report = walk_forward_report({}, train_size=args.train_days, test_size=args.test_days)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return

  day_map: dict[date, DayResult] = {}
  for d in days:
    print(f"==> Backtest {d.isoformat()} ...")
    try:
      res = await _run_one(d, from_snap=args.from_snap)
    except Exception as exc:
      print(f"  SKIP {d}: {exc}")
      continue
    if res is None:
      print(f"  SKIP {d}: no stats")
      continue
    day_map[d] = res
    print(f"  trades={res.trades} wr={res.win_rate:.1%} pnl={res.pnl:.2f}")

  report = walk_forward_report(
    day_map, train_size=args.train_days, test_size=args.test_days
  )
  report["per_day"] = {
    d.isoformat(): {
      "trades": r.trades,
      "wins": r.wins,
      "pnl": r.pnl,
      "win_rate": r.win_rate,
    }
    for d, r in sorted(day_map.items())
  }
  Path(args.report).parent.mkdir(parents=True, exist_ok=True)
  Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
  print("\n=== Walk-forward OOS ===")
  print(json.dumps(report.get("aggregate_oos"), indent=2))
  print(f"Report: {args.report}")


if __name__ == "__main__":
  asyncio.run(main())
