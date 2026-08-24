"""Phase 5–8 unit tests: vol, EV, thesis, families, walk-forward."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from algocrypto.backtest.walk_forward import DayResult, rolling_windows, walk_forward_report
from algocrypto.journal.analytics import StrategyLearner
from algocrypto.models.events import Candle, CandleInterval
from algocrypto.option_data.vol_model import (
  ExpiryBucket,
  IVHistory,
  IVRegime,
  build_vol_snapshot,
  classify_iv_regime,
  expiry_bucket,
  iv_setup_quality,
  minutes_to_expiry,
  realized_vol_from_candles,
  tte_entry_adjustments,
)
from algocrypto.position.thesis import assess_thesis, build_thesis_from_signal
from algocrypto.strategy.families import health_score_from_stats, strategy_family
from algocrypto.trading.ev_engine import estimate_ev


def _candles(n: int = 80, start: float = 100.0) -> list[Candle]:
  out = []
  px = start
  ts0 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
  for i in range(n):
    px = px * (1.0 + (0.001 if i % 2 == 0 else -0.0008))
    out.append(
      Candle(
        instrument_token="BTC",
        ts=ts0 + timedelta(minutes=i),
        open=Decimal(str(px)),
        high=Decimal(str(px * 1.001)),
        low=Decimal(str(px * 0.999)),
        close=Decimal(str(px)),
        volume=100,
        interval=CandleInterval.M1,
      )
    )
  return out


def test_iv_regime_and_buckets():
  assert classify_iv_regime(iv_change=0.05) == IVRegime.IV_EXPANDING
  assert classify_iv_regime(iv_change=-0.05) == IVRegime.IV_CONTRACTING
  assert expiry_bucket(800) == ExpiryBucket.GT_12H
  assert expiry_bucket(20) == ExpiryBucket.LT_30M
  adj = tte_entry_adjustments(ExpiryBucket.LT_30M)
  assert adj["size_mult"] < 1.0
  assert adj["min_confidence_add"] > 0


def test_vol_snapshot_marks_unavailable():
  hist = IVHistory(maxlen=20)
  for v in [0.4, 0.42, 0.45, 0.44, 0.46, 0.5, 0.48, 0.47, 0.49, 0.51, 0.52]:
    hist.push(v)
  snap = build_vol_snapshot(
    iv=0.55,
    history=hist,
    candles=_candles(),
    spot=100000.0,
    expiry=date.today() + timedelta(days=1),
  )
  assert snap.iv_regime != IVRegime.IV_UNKNOWN or snap.iv_change is not None
  assert snap.time_to_expiry_minutes is not None
  assert snap.realized_vol is not None or "realized_vol" in snap.unavailable


def test_realized_vol_needs_data():
  assert realized_vol_from_candles(_candles(3)) is None
  assert realized_vol_from_candles(_candles(80)) is not None


def test_iv_setup_quality_soft():
  adj, why = iv_setup_quality(
    regime=IVRegime.IV_EXPANDING,
    setup_family="Breakout",
    iv_rank=50,
  )
  assert adj > 0
  adj2, _ = iv_setup_quality(
    regime=IVRegime.IV_CONTRACTING,
    setup_family="Breakout",
    iv_rank=50,
  )
  assert adj2 < 0


def test_minutes_to_expiry_positive():
  m = minutes_to_expiry(date.today() + timedelta(days=1))
  assert m is not None and m > 0


def test_estimate_ev_uses_prior_not_score():
  high = estimate_ev(
    rule_score=95,
    strategy="vwap_reclaim",
    entry_premium_usd=10,
    fees_usd=0.2,
    expected_slippage_usd=0.1,
    learner_snapshot={"stats": {}},
    min_ev=-100,
  )
  low = estimate_ev(
    rule_score=50,
    strategy="vwap_reclaim",
    entry_premium_usd=10,
    fees_usd=0.2,
    expected_slippage_usd=0.1,
    learner_snapshot={"stats": {}},
    min_ev=-100,
  )
  # Same prior → same EV regardless of rule_score
  assert abs(high.expected_value - low.expected_value) < 1e-9
  assert high.detail["pwin_source"] == "uninformative_prior"


def test_estimate_ev_blocks_negative():
  # Force poor empirical WR with enough trades
  snap = {
    "stats": {
      "vwap_reclaim": {
        "trades": 20,
        "win_rate": 0.15,
        "average_win": 1,
        "average_loss": -5,
      }
    }
  }
  ev = estimate_ev(
    rule_score=90,
    strategy="vwap_reclaim",
    entry_premium_usd=10,
    fees_usd=1,
    expected_slippage_usd=1,
    learner_snapshot=snap,
    adverse_pct=0.2,
    reward_pct=0.05,
    min_ev=0.0,
    min_trades_for_pwin=8,
  )
  assert ev.eligible is False
  assert ev.reason == "negative_expected_value"


def test_thesis_degrades_on_vwap_flip():
  thesis = build_thesis_from_signal(
    side="CE",
    strategy="vwap_trend",
    regime_primary="trending_up",
    spot=Decimal("100"),
    vwap=Decimal("99"),
    iv_regime="IV_EXPANDING",
  )
  ok = assess_thesis(
    thesis,
    spot=Decimal("101"),
    vwap=Decimal("99"),
    regime_primary="trending_up",
    structure="hhhl",
    iv_regime="IV_EXPANDING",
    cfg={"thesis_degrade_below": 40, "thesis_exit_enabled": True},
  )
  assert ok.degraded is False
  bad = assess_thesis(
    thesis,
    spot=Decimal("95"),
    vwap=Decimal("99"),
    regime_primary="trending_down",
    structure="lllh",
    iv_regime="IV_CONTRACTING",
    cfg={"thesis_degrade_below": 40, "thesis_exit_enabled": True},
  )
  assert bad.degraded is True
  assert bad.score < 40


def test_strategy_family_and_health():
  assert strategy_family("vwap_reclaim") == "VWAP"
  assert strategy_family("opening_range_breakout") == "Breakout"
  hs, label = health_score_from_stats({"trades": 2, "win_rate": 0}, min_trades=10)
  assert label == "insufficient_sample"
  hs2, label2 = health_score_from_stats(
    {
      "trades": 20,
      "win_rate": 0.2,
      "expectancy": -5,
      "average_win": 1,
      "average_loss": -10,
      "profit_factor": 0.3,
    },
    min_trades=10,
  )
  assert label2 == "weak"
  assert hs2 < 40


def test_learner_rich_stats():
  learner = StrategyLearner(path=None, demote_after_losses=5, min_trades_health=5)
  for i in range(6):
    learner.record_trade(
      "vwap_reclaim",
      10 if i % 2 == 0 else -8,
      regime="trending_up",
      underlying="BTC",
      expiry_bucket="4-12h",
      mfe=0.5,
      mae=-0.2,
    )
  snap = learner.snapshot()
  st = snap["stats"]["vwap_reclaim"]
  assert st["trades"] == 6
  assert "average_win" in st
  assert "by_regime" in st
  assert st["family"] == "VWAP"
  assert "health_score" in st


def test_walk_forward_windows():
  days = [date(2026, 8, d) for d in range(1, 8)]
  wins = rolling_windows(days, train_size=3, test_size=1)
  assert len(wins) >= 3
  day_map = {
    d: DayResult(day=d, trades=2, wins=1, pnl=1.0, win_rate=0.5) for d in days
  }
  report = walk_forward_report(day_map, train_size=3, test_size=1)
  assert report["folds"]
  assert "aggregate_oos" in report
