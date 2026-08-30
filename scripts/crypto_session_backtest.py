#!/usr/bin/env python3
"""Replay last N hours of BTC candles through feature + exit logic (crypto options sim)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from algocrypto.config import AppConfig
from algocrypto.features.engine import FeatureEngine
from algocrypto.features.indicators import aggregate_from_m5, aggregate_m1_clock
from algocrypto.market_data.atr import approx_atr
from algocrypto.models.events import Bias, Candle, CandleInterval, FeatureSnapshot, MarketRegime, MarketRegime, CandidateSignal
from algocrypto.position.exit_rules import evaluate_momentum_exit
from algocrypto.regime.classifier import RegimeClassifier
from algocrypto.scanner.library import build_strategy_scanners
from algocrypto.strategy.router import StrategyRouter
from algocrypto.features.crypto_scaling import crypto_entry_allowed
from algocrypto.quality.gate import QualityGate
from algocrypto.contract_selector.selector import ContractUniverse
from algocrypto.models.events import Instrument, OptionState


IST = timezone(timedelta(hours=5, minutes=30))
LOT_SIZE = Decimal("0.001")
DELTA = Decimal("0.52")
THETA_PER_MIN = Decimal("0.00025")
FEES_CFG = {
  "taker_fee_rate": 0.0003,
  "gst_rate": 0.18,
  "premium_fee_cap_pct": 0.035,
}


@dataclass
class SimPosition:
  side: str
  setup: str
  entry_ts: datetime
  entry_spot: Decimal
  entry_premium: Decimal
  strike: Decimal
  lots: int
  mfe: Decimal = Decimal("0")
  mae: Decimal = Decimal("0")


@dataclass
class SimTrade:
  setup: str
  side: str
  entry_ts: datetime
  exit_ts: datetime
  entry_premium: Decimal
  exit_premium: Decimal
  gross_pnl: Decimal
  fees: Decimal
  net_pnl: Decimal
  exit_reason: str
  hold_seconds: int


@dataclass
class BacktestState:
  trades: list[SimTrade] = field(default_factory=list)
  open_pos: SimPosition | None = None
  last_entry_ts: datetime | None = None
  cooldown_sec: int = 180


def aggregate_m1(m1: list[Candle], minutes: int) -> list[Candle]:
  return aggregate_m1_clock(m1, minutes)


class ReplayMarketData:
  def __init__(
    self,
    m1_all: list[Candle],
    m3_all: list[Candle],
    m5_all: list[Candle],
  ) -> None:
    self._m1_all = m1_all
    self._m3_all = m3_all
    self._m5_all = m5_all
    self._m1: list[Candle] = []
    self._m3: list[Candle] = []
    self._m5: list[Candle] = []
    self._spot: Decimal | None = None

  def set_window(self, session_start: datetime, end_ts: datetime) -> None:
    self._m1 = [c for c in self._m1_all if session_start <= c.ts <= end_ts]
  # Prefer DB candles; fall back to clock-aligned aggregation from 1m.
    db_m3 = [c for c in self._m3_all if session_start <= c.ts <= end_ts]
    db_m5 = [c for c in self._m5_all if session_start <= c.ts <= end_ts]
    self._m3 = db_m3 if db_m3 else aggregate_m1_clock(self._m1, 3)
    self._m5 = db_m5 if db_m5 else aggregate_m1_clock(self._m1, 5)
    self._spot = self._m1[-1].close if self._m1 else None

  @property
  def spot_ltp(self) -> Decimal | None:
    return self._spot

  def session_vwap_value(self) -> Decimal | None:
    from algocrypto.market_data.vwap import session_vwap

    return session_vwap(self._m1)

  def candles(self, interval: CandleInterval) -> list[Candle]:
    if interval == CandleInterval.M1:
      return list(self._m1)
    if interval == CandleInterval.M3:
      return list(self._m3)
    if interval == CandleInterval.M5:
      return list(self._m5)
    return []


def estimate_premium(spot: Decimal, strike: Decimal, side: str) -> Decimal:
  intrinsic_ce = max(spot - strike, Decimal("0"))
  intrinsic_pe = max(strike - spot, Decimal("0"))
  if side == "CE":
    return intrinsic_ce + Decimal("95")
  return intrinsic_pe + Decimal("62")


def premium_at_spot(
  entry_spot: Decimal,
  spot: Decimal,
  entry_premium: Decimal,
  side: str,
  held_min: float,
) -> Decimal:
  move = spot - entry_spot
  if side == "CE":
    change = DELTA * move
  else:
    change = -DELTA * move
  theta = entry_premium * THETA_PER_MIN * Decimal(str(held_min))
  return max(entry_premium + change - theta, Decimal("1"))


def option_fee(spot: Decimal, premium: Decimal, lots: int) -> Decimal:
  notional = spot * lots * LOT_SIZE
  prem = premium * lots * LOT_SIZE
  raw = min(notional * Decimal("0.0003"), prem * Decimal("0.035"))
  return raw * Decimal("1.18")


def atm_strike(spot: Decimal) -> Decimal:
  step = Decimal("200")
  return (spot / step).quantize(Decimal("1")) * step


def mock_universe(spot: Decimal) -> ContractUniverse:
  strike = atm_strike(spot)
  inst_ce = Instrument(
    token="SIM-CE",
    tsym=f"C-BTC-{int(strike)}-SIM",
    exchange="DELTA",
    strike=strike,
    option_type="CE",
    underlying="BTC",
    contract_size=LOT_SIZE,
  )
  inst_pe = Instrument(
    token="SIM-PE",
    tsym=f"P-BTC-{int(strike)}-SIM",
    exchange="DELTA",
    strike=strike,
    option_type="PE",
    underlying="BTC",
    contract_size=LOT_SIZE,
  )
  return ContractUniverse(
    spot=spot,
    atm_strike=strike,
    underlying="BTC",
    instruments=[inst_ce, inst_pe],
    atm_ce=inst_ce,
    atm_pe=inst_pe,
  )


class ReplayFeatureEngine(FeatureEngine):
  def __init__(self, config: AppConfig, market_data: ReplayMarketData) -> None:
    super().__init__(config, market_data)  # type: ignore[arg-type]
    self._replay_md = market_data

  def compute(self) -> FeatureSnapshot:
    snap = super().compute()
    # Patch ts from last candle
    if self._replay_md._m1:
      ts = self._replay_md._m1[-1].ts
      return FeatureSnapshot(
        ts=ts,
        nifty_spot=snap.nifty_spot,
        session_vwap=snap.session_vwap,
        bias_5m=snap.bias_5m,
        setup_3m=snap.setup_3m,
        trigger_1m=snap.trigger_1m,
        extra=snap.extra,
      )
    return snap


async def load_candles(hours: float) -> tuple[list[Candle], list[Candle], list[Candle]]:
  from algocrypto.db.connection import init_pool, get_pool

  await init_pool()
  pool = get_pool()
  since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)

  async def _fetch(table: str, interval: CandleInterval) -> list[Candle]:
    async with pool.acquire() as conn:
      if hours <= 0:
        rows = await conn.fetch(
          f"""
          SELECT ts, open, high, low, close, volume
          FROM {table}
          WHERE instrument_token = 'BTCUSD'
          ORDER BY ts
          """
        )
      else:
        rows = await conn.fetch(
          f"""
          SELECT ts, open, high, low, close, volume
          FROM {table}
          WHERE instrument_token = 'BTCUSD' AND ts >= $1
          ORDER BY ts
          """,
          since,
        )
    out: list[Candle] = []
    for r in rows:
      out.append(
        Candle(
          instrument_token="BTCUSD",
          ts=r["ts"],
          open=Decimal(str(r["open"])),
          high=Decimal(str(r["high"])),
          low=Decimal(str(r["low"])),
          close=Decimal(str(r["close"])),
          volume=int(r["volume"] or 0),
          interval=interval,
        )
      )
    return out

  m1 = await _fetch("candles_1m", CandleInterval.M1)
  m3 = await _fetch("candles_3m", CandleInterval.M3)
  m5 = await _fetch("candles_5m", CandleInterval.M5)
  return m1, m3, m5


def _crypto_entry_allowed(
  config: AppConfig,
  features: FeatureSnapshot,
  regime: MarketRegime,
  signal: CandidateSignal,
) -> tuple[bool, str]:
  return crypto_entry_allowed(config.strategy, features, regime, signal)


def run_replay(
  m1_all: list[Candle],
  m3_all: list[Candle],
  m5_all: list[Candle],
  config: AppConfig,
  scan_every: int = 2,
) -> BacktestState:
  md = ReplayMarketData(m1_all, m3_all, m5_all)
  features = ReplayFeatureEngine(config, md)
  regime_cls = RegimeClassifier(config)
  quality = QualityGate(config)
  scanners = build_strategy_scanners(config)
  router = StrategyRouter(config, scanners, quality)
  router_cfg = config.strategy.get("router", {})
  router._min_confidence = int(router_cfg.get("min_confidence", 80))
  router._log_every = False
  exit_cfg = config.position_exit
  state = BacktestState(cooldown_sec=180)
  capital = Decimal("250")
  min_bars = 120

  for i in range(min_bars, len(m1_all)):
    ts = m1_all[i].ts
    session_start = ts - timedelta(hours=8)
    md.set_window(session_start, ts)
    window = md.candles(CandleInterval.M1)
    if len(window) < 60:
      continue

    # Exit tick every minute
    if state.open_pos:
      pos = state.open_pos
      held = (ts - pos.entry_ts).total_seconds()
      spot = window[-1].close
      ltp = premium_at_spot(
        pos.entry_spot,
        spot,
        pos.entry_premium,
        pos.side,
        held / 60.0,
      )
      mfe_pts = max(pos.mfe, ltp - pos.entry_premium)
      pos.mfe = mfe_pts
      pos.mae = min(pos.mae, ltp - pos.entry_premium)
      atr = approx_atr(window, 14)
      decision = evaluate_momentum_exit(
        option_side=pos.side,
        entry_price=pos.entry_premium,
        entry_ts=pos.entry_ts,
        current_ltp=ltp,
        mfe_points=pos.mfe,
        market_data=md,  # type: ignore[arg-type]
        cfg=exit_cfg,
        force_exit=False,
        now=ts,
        atr=atr,
        lots=pos.lots,
        contract_size=LOT_SIZE,
        entry_fee_usd=option_fee(pos.entry_spot, pos.entry_premium, pos.lots),
        fees_cfg=FEES_CFG,
      )
      if decision.should_exit:
        gross = (ltp - pos.entry_premium) * pos.lots * LOT_SIZE
        fees = option_fee(pos.entry_spot, pos.entry_premium, pos.lots) + option_fee(
          spot, ltp, pos.lots
        )
        state.trades.append(
          SimTrade(
            setup=pos.setup,
            side=pos.side,
            entry_ts=pos.entry_ts,
            exit_ts=ts,
            entry_premium=pos.entry_premium,
            exit_premium=ltp,
            gross_pnl=gross,
            fees=fees,
            net_pnl=gross - fees,
            exit_reason=decision.reason or "exit",
            hold_seconds=int(held),
          )
        )
        state.open_pos = None

    if i % scan_every != 0 or state.open_pos:
      continue
    if state.last_entry_ts and (ts - state.last_entry_ts).total_seconds() < state.cooldown_sec:
      continue

    feat = features.compute()
    m3 = md.candles(CandleInterval.M3)
    m5 = md.candles(CandleInterval.M5)
    reg = regime_cls.classify(feat, window, m5, now=ts)
    if not reg.trade_allowed:
      continue

    spot = feat.nifty_spot or window[-1].close
    universe = mock_universe(spot)
    from algocrypto.models.events import OptionState

    prem_ce = estimate_premium(spot, universe.atm_strike, "CE")
    prem_pe = estimate_premium(spot, universe.atm_strike, "PE")
    option_states = {
      universe.atm_ce.token: OptionState(
        instrument_token=universe.atm_ce.token,
        tsym=universe.atm_ce.tsym,
        ltp=prem_ce,
        spread_pct=Decimal("2"),
      ),
      universe.atm_pe.token: OptionState(
        instrument_token=universe.atm_pe.token,
        tsym=universe.atm_pe.tsym,
        ltp=prem_pe,
        spread_pct=Decimal("2"),
      ),
    }
    options_by_strategy = {s.name: option_states for s in scanners}
    decision, sig = router.route(feat, reg, universe, options_by_strategy)
    if sig is None:
      continue

    allowed, gate_reason = _crypto_entry_allowed(config, feat, reg, sig)
    if not allowed:
      continue
    lots = 1
    entry_prem = prem_ce if sig.side == "CE" else prem_pe
    state.open_pos = SimPosition(
      side=sig.side,
      setup=sig.setup_type,
      entry_ts=ts,
      entry_spot=spot,
      entry_premium=entry_prem,
      strike=universe.atm_strike,
      lots=lots,
    )
    state.last_entry_ts = ts

  return state


def summarize(state: BacktestState) -> dict:
  trades = state.trades
  if not trades:
    return {"trades": 0, "message": "No trades in window"}
  net = sum(t.net_pnl for t in trades)
  gross = sum(t.gross_pnl for t in trades)
  fees = sum(t.fees for t in trades)
  wins = sum(1 for t in trades if t.net_pnl > 0)
  by_setup: dict[str, list] = {}
  by_exit: dict[str, list] = {}
  for t in trades:
    by_setup.setdefault(t.setup, []).append(t)
    by_exit.setdefault(t.exit_reason, []).append(t)
  return {
    "trades": len(trades),
    "wins": wins,
    "losses": len(trades) - wins,
    "win_rate_pct": round(100 * wins / len(trades), 1),
    "gross_pnl_usd": float(gross),
    "fees_usd": float(fees),
    "net_pnl_usd": float(net),
    "avg_net_usd": float(net / len(trades)),
    "by_setup": {
      k: {
        "n": len(v),
        "net": float(sum(x.net_pnl for x in v)),
      }
      for k, v in by_setup.items()
    },
    "by_exit_reason": {
      k: {
        "n": len(v),
        "net": float(sum(x.net_pnl for x in v)),
      }
      for k, v in by_exit.items()
    },
    "trade_log": [
      {
        "setup": t.setup,
        "side": t.side,
        "net": float(t.net_pnl),
        "exit": t.exit_reason,
        "hold_s": t.hold_seconds,
      }
      for t in trades
    ],
  }


async def main() -> None:
  parser = argparse.ArgumentParser(description="Crypto session backtest from DB candles")
  parser.add_argument("--hours", type=float, default=24.0, help="0 = all DB candles")
  parser.add_argument("--scan-every", type=int, default=2, help="Minutes between entry scans")
  parser.add_argument("--out", type=str, default="reports/crypto_backtest_24h.json")
  args = parser.parse_args()

  m1, m3, m5 = await load_candles(args.hours)
  if len(m1) < 150:
    print(f"ERROR: only {len(m1)} 1m bars — need engine running to collect candles")
    sys.exit(1)

  config = AppConfig()
  state = run_replay(m1, m3, m5, config, scan_every=args.scan_every)
  report = summarize(state)
  report["bars"] = len(m1)
  report["from"] = m1[0].ts.isoformat()
  report["to"] = m1[-1].ts.isoformat()
  report["config_version"] = config.strategy.get("strategy_version")

  out_path = ROOT / args.out
  out_path.parent.mkdir(parents=True, exist_ok=True)
  out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
  print(json.dumps(report, indent=2))
  print(f"\nWritten: {out_path}")


if __name__ == "__main__":
  asyncio.run(main())
