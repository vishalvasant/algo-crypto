#!/usr/bin/env python3
"""Today's multi-TF candle analysis: options vs futures P&L scenarios."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
CAPITAL = Decimal("250")
LEV = Decimal("100")
LOT_SIZE = Decimal("0.001")
FUT_TAKER = Decimal("0.0005")
OPT_TAKER = Decimal("0.0003")
PREM_CAP = Decimal("0.035")
GST = Decimal("1.18")
STRIKE = Decimal("77600")


@dataclass
class Scenario:
  name: str
  category: str
  entry_spot: Decimal
  exit_spot: Decimal
  side: str  # CE long opt / PE long opt / FUT_LONG / FUT_SHORT
  entry_premium: Decimal | None = None


def fut_fees(notional_entry: Decimal, notional_exit: Decimal) -> Decimal:
  return (notional_entry + notional_exit) * FUT_TAKER * GST


def opt_fees(spot: Decimal, prem: Decimal, lots: int) -> Decimal:
  notional = spot * lots * LOT_SIZE
  premium = prem * lots * LOT_SIZE
  return min(notional * OPT_TAKER, premium * PREM_CAP) * GST


def estimate_premium(spot: Decimal, strike: Decimal, side: str) -> Decimal:
  """Rough ATM-ish premium from today's observed trades (~$192 CE, ~$125 PE @ 77640)."""
  intrinsic_ce = max(spot - strike, Decimal("0"))
  intrinsic_pe = max(strike - spot, Decimal("0"))
  # Time value ~ $90-100 for CE, ~$60-70 for PE near ATM (from trade data)
  if side == "CE":
    return intrinsic_ce + Decimal("95")
  return intrinsic_pe + Decimal("62")


def estimate_exit_premium(
  entry_spot: Decimal,
  exit_spot: Decimal,
  entry_premium: Decimal,
  side: str,
  delta: Decimal = Decimal("0.52"),
) -> Decimal:
  """Delta + mild theta bleed for short holds (~0.02%/min on premium)."""
  move = exit_spot - entry_spot
  if side == "CE":
    prem_change = delta * move
  else:
    prem_change = -delta * move
  # Theta: ~0.3% premium decay per 10 min hold (observed in chop)
  theta_drag = entry_premium * Decimal("0.0003")
  exit_p = entry_premium + prem_change - theta_drag
  return max(exit_p, Decimal("1"))


def run_scenario(s: Scenario) -> dict:
  move_pct = float((s.exit_spot - s.entry_spot) / s.entry_spot * 100)
  out: dict = {
    "scenario": s.name,
    "category": s.category,
    "btc": f"${float(s.entry_spot):,.0f} → ${float(s.exit_spot):,.0f} ({move_pct:+.2f}%)",
  }

  if s.side in ("CE", "PE"):
    ep = s.entry_premium or estimate_premium(s.entry_spot, STRIKE, s.side)
    xp = estimate_exit_premium(s.entry_spot, s.exit_spot, ep, s.side)
    lots = int(CAPITAL / (ep * LOT_SIZE))
    deployed = ep * lots * LOT_SIZE
    gross = (xp - ep) * lots * LOT_SIZE
    fees = opt_fees(s.entry_spot, ep, lots) + opt_fees(s.exit_spot, xp, lots)
    net = gross - fees
    roi = float(net / deployed * 100) if deployed else 0
    out["instrument"] = f"Long {s.side} @ strike {STRIKE}"
    out["capital"] = f"${float(deployed):.2f}"
    out["lots"] = lots
    out["premium"] = f"${float(ep):.1f} → ${float(xp):.1f}"
    out["gross_pnl"] = f"${float(gross):.2f}"
    out["fees"] = f"${float(fees):.2f}"
    out["net_pnl"] = f"${float(net):.2f}"
    out["roi_pct"] = f"{roi:+.1f}%"
    out["_net"] = float(net)
    return out

  # Futures
  is_long = s.side == "FUT_LONG"
  notional = CAPITAL * LEV
  btc = notional / s.entry_spot
  lots = int(btc / LOT_SIZE)
  move = s.exit_spot - s.entry_spot
  gross = btc * move if is_long else -btc * move
  fees = fut_fees(s.entry_spot * btc, s.exit_spot * btc)
  net = gross - fees
  roi = float(net / CAPITAL * 100)
  out["instrument"] = "Long BTC perp" if is_long else "Short BTC perp"
  out["capital"] = f"${float(CAPITAL):.2f} (margin)"
  out["lots"] = lots
  out["notional"] = f"${float(notional):,.0f}"
  out["gross_pnl"] = f"${float(gross):.2f}"
  out["fees"] = f"${float(fees):.2f}"
  out["net_pnl"] = f"${float(net):.2f}"
  out["roi_pct"] = f"{roi:+.1f}%"
  out["_net"] = float(net)
  return out


async def load_candles(conn):
  rows = {}
  for tf, table in [("1m", "candles_1m"), ("3m", "candles_3m"), ("5m", "candles_5m")]:
    data = await conn.fetch(
      f"""
      SELECT ts, open, high, low, close, volume
      FROM {table}
      WHERE instrument_token = 'BTCUSD' AND ts::date = CURRENT_DATE
      ORDER BY ts
      """
    )
    rows[tf] = data
  return rows


async def main():
  from algocrypto.db.connection import init_pool, get_pool

  await init_pool()
  pool = get_pool()

  async with pool.acquire() as conn:
    candles = await load_candles(conn)
    trades = await conn.fetch(
      """
      SELECT CASE WHEN p.tsym LIKE 'C-%' THEN 'CE' ELSE 'PE' END side,
             ct.entry_price, ct.exit_price, ct.pnl, ct.entry_ts, ct.exit_ts,
             ct.signal_snapshot
      FROM closed_trades ct JOIN positions p ON p.id = ct.position_id
      WHERE ct.exit_ts::date = CURRENT_DATE ORDER BY ct.entry_ts
      """
    )

  m1 = candles["1m"]
  if not m1:
    print(json.dumps({"error": "no candle data"}))
    return

  day_open = Decimal(str(m1[0]["open"]))
  day_close = Decimal(str(m1[-1]["close"]))
  day_low = min(Decimal(str(r["low"])) for r in m1)
  day_high = max(Decimal(str(r["high"])) for r in m1)
  day_range = day_high - day_low
  day_range_pct = float(day_range / day_low * 100)

  # Key timestamps from candles
  low_bar = min(m1, key=lambda r: Decimal(str(r["low"])))
  high_bar = max(m1, key=lambda r: Decimal(str(r["high"])))
  low_ts = low_bar["ts"].astimezone(IST).strftime("%H:%M IST")
  high_ts = high_bar["ts"].astimezone(IST).strftime("%H:%M IST")

  scenarios: list[Scenario] = []

  # --- POSITIVE (best realistic directional plays) ---
  scenarios += [
    Scenario("Best long: buy day LOW → sell day HIGH", "positive", day_low, day_high, "FUT_LONG"),
    Scenario("Best long: buy day LOW → sell day HIGH", "positive", day_low, day_high, "CE"),
    Scenario("Best short: sell day HIGH → buy day LOW", "positive", day_high, day_low, "FUT_SHORT"),
    Scenario("Best short: sell day HIGH → buy day LOW", "positive", day_high, day_low, "PE"),
    Scenario("Open → High (morning rally)", "positive", day_open, day_high, "FUT_LONG"),
    Scenario("Open → High (morning rally)", "positive", day_open, day_high, "CE"),
    Scenario("Open → Low (morning fade)", "positive", day_open, day_low, "FUT_SHORT"),
    Scenario("Open → Low (morning fade)", "positive", day_open, day_low, "PE"),
    Scenario("Mid session +0.5% rally", "positive", day_close, day_close * Decimal("1.005"), "FUT_LONG"),
    Scenario("Mid session +0.5% rally", "positive", day_close, day_close * Decimal("1.005"), "CE"),
    Scenario("Mid session +1.0% rally (strong trend)", "positive", day_close, day_close * Decimal("1.01"), "FUT_LONG"),
    Scenario("Mid session +1.0% rally (strong trend)", "positive", day_close, day_close * Decimal("1.01"), "CE"),
  ]

  # --- NEGATIVE (wrong direction / worst timing) ---
  scenarios += [
    Scenario("Worst long: buy HIGH → sell LOW", "negative", day_high, day_low, "FUT_LONG"),
    Scenario("Worst long: buy HIGH → sell LOW", "negative", day_high, day_low, "CE"),
    Scenario("Worst short: sell LOW → buy HIGH", "negative", day_low, day_high, "FUT_SHORT"),
    Scenario("Worst short: sell LOW → buy HIGH", "negative", day_low, day_high, "PE"),
    Scenario("Open → Close (held all day long)", "negative", day_open, day_close, "FUT_LONG"),
    Scenario("Open → Close (held all day long)", "negative", day_open, day_close, "CE"),
    Scenario("Mid session −0.5% drop", "negative", day_close, day_close * Decimal("0.995"), "FUT_LONG"),
    Scenario("Mid session −0.5% drop", "negative", day_close, day_close * Decimal("0.995"), "CE"),
    Scenario("Mid session −0.5% (short loses)", "negative", day_close, day_close * Decimal("1.005"), "FUT_SHORT"),
    Scenario("Mid session −0.5% (short loses)", "negative", day_close, day_close * Decimal("1.005"), "PE"),
    Scenario("Mid session −1.0% crash", "negative", day_close, day_close * Decimal("0.99"), "FUT_LONG"),
    Scenario("Mid session −1.0% crash", "negative", day_close, day_close * Decimal("0.99"), "CE"),
  ]

  # Actual algo trade windows (from DB)
  import json as _json

  for i, t in enumerate(trades, 1):
    snap = t["signal_snapshot"]
    if isinstance(snap, str):
      snap = _json.loads(snap)
    es = Decimal(str(snap.get("nifty_spot", day_open)))
    # approximate exit spot from premium move isn't available — use candle at exit
    exit_ts = t["exit_ts"]
    exit_row = min(m1, key=lambda r, ts=exit_ts: abs((r["ts"] - ts).total_seconds()))
    xs = Decimal(str(exit_row["close"]))
    side = t["side"]
    scenarios.append(
      Scenario(
        f"Actual algo trade #{i} ({side})",
        "actual_loss",
        es,
        xs,
        side,
        entry_premium=Decimal(str(t["entry_price"])),
      )
    )
    scenarios.append(
      Scenario(
        f"Actual trade #{i} if reversed",
        "actual_if_reversed",
        es,
        xs,
        "PE" if side == "CE" else "CE",
      )
    )

  results = [run_scenario(s) for s in scenarios]

  def summarize(cat: str, instrument_filter: str) -> dict:
    subset = [
      r
      for r in results
      if r["category"] == cat and instrument_filter in r.get("instrument", "")
    ]
    nets = [r["_net"] for r in subset]
    return {
      "count": len(nets),
      "total_net": round(sum(nets), 2),
      "avg_net": round(sum(nets) / len(nets), 2) if nets else 0,
      "best": round(max(nets), 2) if nets else 0,
      "worst": round(min(nets), 2) if nets else 0,
    }

  # Pairwise compare matched scenarios
  pairs = []
  for r in results:
    if "FUT" in r.get("instrument", ""):
      continue
    base = r["scenario"]
    fut = next(
      (
        x
        for x in results
        if x["scenario"] == base
        and x is not r
        and ("perp" in x.get("instrument", ""))
      ),
      None,
    )
    if fut:
      pairs.append(
        {
          "scenario": base,
          "category": r["category"],
          "options_net": r["_net"],
          "futures_net": fut["_net"],
          "winner": "Options"
          if r["_net"] > fut["_net"]
          else "Futures"
          if fut["_net"] > r["_net"]
          else "Tie",
        }
      )

  opt_wins = sum(1 for p in pairs if p["winner"] == "Options")
  fut_wins = sum(1 for p in pairs if p["winner"] == "Futures")

  # TF summary
  tf_summary = {}
  for tf, rows in candles.items():
    if not rows:
      continue
    lo = min(Decimal(str(r["low"])) for r in rows)
    hi = max(Decimal(str(r["high"])) for r in rows)
    tf_summary[tf] = {
      "bars": len(rows),
      "low": float(lo),
      "high": float(hi),
      "range_usd": float(hi - lo),
      "range_pct": round(float((hi - lo) / lo * 100), 3),
    }

  output = {
    "date": datetime.now(IST).strftime("%Y-%m-%d"),
    "capital_usd": float(CAPITAL),
    "leverage": "100x",
    "strike_used": float(STRIKE),
    "btc_day": {
      "open": float(day_open),
      "high": float(day_high),
      "high_at": high_ts,
      "low": float(day_low),
      "low_at": low_ts,
      "close": float(day_close),
      "range_usd": float(day_range),
      "range_pct": round(day_range_pct, 3),
    },
    "timeframes": tf_summary,
    "scenario_results": [{k: v for k, v in r.items() if k != "_net"} for r in results],
    "paired_comparison": pairs,
    "summary": {
      "positive_scenarios": {
        "options_ce": summarize("positive", "CE"),
        "futures_long": summarize("positive", "Long BTC"),
      },
      "negative_scenarios": {
        "options_ce_long": summarize("negative", "CE"),
        "futures_long": summarize("negative", "Long BTC"),
      },
      "paired_wins": {"options": opt_wins, "futures": fut_wins, "total_pairs": len(pairs)},
    },
    "verdict": "",
  }

  pos_opt = sum(p["options_net"] for p in pairs if p["category"] == "positive")
  pos_fut = sum(p["futures_net"] for p in pairs if p["category"] == "positive")
  neg_opt = sum(p["options_net"] for p in pairs if p["category"] == "negative")
  neg_fut = sum(p["futures_net"] for p in pairs if p["category"] == "negative")

  if pos_opt > pos_fut and abs(neg_opt) < abs(neg_fut):
    verdict = "Options better for BIG directional wins; futures safer in losses today"
  elif fut_wins > opt_wins:
    verdict = (
      f"Futures win {fut_wins}/{len(pairs)} paired scenarios on today's "
      f"{day_range_pct:.2f}% range — linear P&L beats theta decay in chop"
    )
  else:
    verdict = "Mixed — options win on large moves, futures win on small/wrong moves"

  output["summary"]["positive_total_options"] = round(pos_opt, 2)
  output["summary"]["positive_total_futures"] = round(pos_fut, 2)
  output["summary"]["negative_total_options"] = round(neg_opt, 2)
  output["summary"]["negative_total_futures"] = round(neg_fut, 2)
  output["verdict"] = verdict

  print(json.dumps(output, indent=2))


if __name__ == "__main__":
  asyncio.run(main())
