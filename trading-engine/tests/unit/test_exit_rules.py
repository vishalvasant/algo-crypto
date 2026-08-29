"""Unit tests for momentum / trend-reversal exits."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from algocrypto.models.events import Candle, CandleInterval
from algocrypto.position.exit_rules import evaluate_momentum_exit


def _cfg(**overrides):
  cfg = {
    "min_hold_seconds": 20,
    "max_hold_minutes": 0,
    "bias_flip_exit": True,
    "bias_flip_buffer_points": 8,
    "trend_reversal_min_hold_seconds": 20,
    "trend_reversal_require_m1_close": False,
    "trend_reversal_only_if_underwater": False,
    "trend_reversal_defer_profit_pct": 0,
    "adverse_move_pct_from_entry": 12,
    "min_profit_before_trail_pct": 18,
    "trail_giveback_pct": 35,
  }
  cfg.update(overrides)
  return cfg


def _md(*, spot: Decimal, vwap: Decimal, m1_close: Decimal | None = None):
  md = MagicMock()
  md.spot_ltp = spot
  md.session_vwap_value = vwap
  if m1_close is not None:
    bar = Candle(
      instrument_token="BTC",
      ts=datetime.now(tz=timezone.utc),
      open=m1_close,
      high=m1_close,
      low=m1_close,
      close=m1_close,
      volume=0,
      interval=CandleInterval.M1,
    )
    md.candles.return_value = [bar]
  else:
    md.candles.return_value = []
  return md


def test_trend_reversal_skips_when_in_profit_and_underwater_only():
  """Winners defer to trail — no VWAP reversal exit while premium is green."""
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("108"),
    mfe_points=Decimal("10"),
    market_data=_md(spot=Decimal("24100"), vwap=Decimal("24150")),
    cfg=_cfg(
      bias_flip_buffer_points=8,
      trend_reversal_only_if_underwater=True,
      min_profit_before_trail_pct=18,
    ),
    force_exit=False,
  )
  assert not decision.should_exit


def test_trend_reversal_exits_underwater_on_bias_flip():
  md = _md(spot=Decimal("24100"), vwap=Decimal("24150"))
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=120)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("95"),
    mfe_points=Decimal("2"),
    market_data=md,
    cfg=_cfg(bias_flip_buffer_points=8, trend_reversal_only_if_underwater=True),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "trend_reversal"


def test_trend_reversal_respects_vwap_buffer():
  md = _md(spot=Decimal("24148"), vwap=Decimal("24150"))
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=120)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99"),
    mfe_points=Decimal("0"),
    market_data=md,
    cfg=_cfg(bias_flip_buffer_points=8),
    force_exit=False,
  )
  assert not decision.should_exit

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99"),
    mfe_points=Decimal("0"),
    market_data=_md(spot=Decimal("24140"), vwap=Decimal("24150")),
    cfg=_cfg(bias_flip_buffer_points=8),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "trend_reversal"


def test_pe_trend_reversal_when_spot_above_vwap():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
  decision = evaluate_momentum_exit(
    option_side="PE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("95"),
    mfe_points=Decimal("0"),
    market_data=_md(spot=Decimal("24200"), vwap=Decimal("24150")),
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "trend_reversal"


def test_min_hold_blocks_immediate_reversal():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99"),
    mfe_points=Decimal("0"),
    market_data=_md(spot=Decimal("24100"), vwap=Decimal("24150")),
    cfg=_cfg(),
    force_exit=False,
  )
  assert not decision.should_exit


def test_trail_before_reversal_on_moderate_profit():
  """Trail arms at 5% MFE and exits before underwater reversal."""
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("103"),
    mfe_points=Decimal("6"),
    market_data=_md(spot=Decimal("24200"), vwap=Decimal("24150")),
    cfg=_cfg(min_profit_before_trail_pct=5, trail_giveback_pct=40),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "momentum_trail"


def test_large_profit_trail_exits_on_giveback():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("115"),
    mfe_points=Decimal("25"),
    market_data=_md(spot=Decimal("24200"), vwap=Decimal("24150")),
    cfg=_cfg(min_profit_before_trail_pct=18),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "momentum_trail"


def test_reversal_uses_m1_close_when_configured():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
  # Tick spot below VWAP but 1m close still above buffer → no exit.
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("95"),
    mfe_points=Decimal("0"),
    market_data=_md(
      spot=Decimal("24100"),
      vwap=Decimal("24150"),
      m1_close=Decimal("24155"),
    ),
    cfg=_cfg(
      bias_flip_buffer_points=8,
      trend_reversal_require_m1_close=True,
    ),
    force_exit=False,
  )
  assert not decision.should_exit
