"""Unit tests for momentum / trend-reversal exits."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from algocrypto.position.exit_rules import evaluate_momentum_exit


def _cfg(**overrides):
  cfg = {
    "min_hold_seconds": 20,
    "max_hold_minutes": 0,
    "bias_flip_exit": True,
    "adverse_move_pct_from_entry": 12,
    "min_profit_before_trail_pct": 18,
    "trail_giveback_pct": 35,
  }
  cfg.update(overrides)
  return cfg


def test_trend_reversal_exits_before_trail_even_in_profit():
  """CE with spot well below VWAP exits on reversal — does not wait for trail."""
  md = MagicMock()
  md.spot_ltp = Decimal("24100")
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("108"),  # +8% — would otherwise trail later
    mfe_points=Decimal("10"),
    market_data=md,
    cfg=_cfg(bias_flip_buffer_points=8),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "trend_reversal"


def test_trend_reversal_respects_vwap_buffer():
  """Within buffer of VWAP — no trend_reversal yet."""
  md = MagicMock()
  md.spot_ltp = Decimal("24148")  # only 2 pts below VWAP
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)

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

  md.spot_ltp = Decimal("24140")  # 10 pts below
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
  assert decision.should_exit
  assert decision.reason == "trend_reversal"


def test_pe_trend_reversal_when_spot_above_vwap():
  md = MagicMock()
  md.spot_ltp = Decimal("24200")
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)

  decision = evaluate_momentum_exit(
    option_side="PE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("95"),
    mfe_points=Decimal("0"),
    market_data=md,
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "trend_reversal"


def test_min_hold_blocks_immediate_reversal():
  md = MagicMock()
  md.spot_ltp = Decimal("24100")
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=5)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99"),
    mfe_points=Decimal("0"),
    market_data=md,
    cfg=_cfg(),
    force_exit=False,
  )
  assert not decision.should_exit


def test_small_profit_does_not_arm_trail():
  """Trail only after large MFE — small winners ride until reverse/adverse."""
  md = MagicMock()
  md.spot_ltp = Decimal("24200")
  md.session_vwap_value = Decimal("24150")  # still bullish for CE
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("108"),  # +8% < 18% trail arm
    mfe_points=Decimal("10"),
    market_data=md,
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit is False


def test_large_profit_trail_exits_on_giveback():
  md = MagicMock()
  md.spot_ltp = Decimal("24200")
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)

  # MFE +25% (≥18%), giveback 35% → floor = 100 + 25*(1-0.35) = 116.25
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("115"),
    mfe_points=Decimal("25"),
    market_data=md,
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "momentum_trail"
