"""Unit tests for Gap-Fix Phase 1 circuit breakers."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from algocrypto.models.events import Bias, CandidateSignal, FeatureSnapshot, OptionState
from algocrypto.risk.circuit_breakers import (
  CircuitSnapshot,
  daily_loss_limit_usd,
  evaluate_circuit_breakers,
  post_trade_halt_reason,
)
from algocrypto.risk.engine import DailyRiskSnapshot, RiskEngine
from algocrypto.risk.states import RiskState


def _snap(**overrides) -> CircuitSnapshot:
  base = dict(
    starting_capital=Decimal("250"),
    realized_pnl=Decimal("0"),
    trade_count=0,
    consecutive_losses=0,
    open_position_count=0,
    flip_count=0,
    flips_last_hour=0,
    losing_trades_last_hour=0,
    kill_switch=False,
    entries_blocked=False,
    risk_state=RiskState.NORMAL,
    flips_disabled=False,
  )
  base.update(overrides)
  return CircuitSnapshot(**base)


def _cfg(**overrides):
  cfg = {
    "daily_loss_limit_amount": 40,
    "daily_loss_limit_pct": 20,
    "max_daily_loss": 0,
    "max_consecutive_losses": 4,
    "max_trades_per_day": 20,
    "max_losing_trades_per_hour": 8,
    "max_positions": 2,
    "max_flips_per_day": 6,
    "max_flips_per_hour": 3,
  }
  cfg.update(overrides)
  return cfg


def test_daily_loss_prefers_amount():
  assert daily_loss_limit_usd(_cfg(), Decimal("250")) == Decimal("40")


def test_daily_loss_uses_pct_when_amount_zero():
  cfg = _cfg(daily_loss_limit_amount=0, max_daily_loss=0, daily_loss_limit_pct=20)
  assert daily_loss_limit_usd(cfg, Decimal("250")) == Decimal("50")


def test_daily_loss_limit_halts():
  d = evaluate_circuit_breakers(
    _cfg(), _snap(realized_pnl=Decimal("-40"))
  )
  assert d.allow_entry is False
  assert d.state == RiskState.HALTED
  assert d.reason == "daily_loss_limit"


def test_consecutive_losses_halts():
  d = evaluate_circuit_breakers(
    _cfg(), _snap(consecutive_losses=4)
  )
  assert d.allow_entry is False
  assert d.reason == "max_consecutive_losses"


def test_max_trades_halts():
  d = evaluate_circuit_breakers(_cfg(), _snap(trade_count=20))
  assert d.allow_entry is False
  assert d.reason == "max_trades_per_day"


def test_max_losing_per_hour_halts():
  d = evaluate_circuit_breakers(
    _cfg(), _snap(losing_trades_last_hour=8)
  )
  assert d.allow_entry is False
  assert d.reason == "max_losing_trades_per_hour"


def test_max_positions_blocks():
  d = evaluate_circuit_breakers(
    _cfg(), _snap(open_position_count=2)
  )
  assert d.allow_entry is False
  assert d.reason == "max_positions"


def test_flip_cap_blocks_flip_only():
  d_normal = evaluate_circuit_breakers(
    _cfg(), _snap(flip_count=6), is_flip=False
  )
  assert d_normal.allow_entry is True
  assert d_normal.allow_flip is False

  d_flip = evaluate_circuit_breakers(
    _cfg(), _snap(flip_count=6), is_flip=True
  )
  assert d_flip.allow_entry is False
  assert d_flip.reason == "max_flips_per_day"


def test_zero_limits_disabled():
  cfg = _cfg(
    daily_loss_limit_amount=0,
    daily_loss_limit_pct=0,
    max_daily_loss=0,
    max_consecutive_losses=0,
    max_trades_per_day=0,
    max_losing_trades_per_hour=0,
    max_positions=0,
    max_flips_per_day=0,
    max_flips_per_hour=0,
  )
  d = evaluate_circuit_breakers(
    cfg,
    _snap(
      realized_pnl=Decimal("-999"),
      consecutive_losses=99,
      trade_count=999,
      flip_count=99,
    ),
  )
  assert d.allow_entry is True


def test_post_trade_halt_reason():
  cfg = _cfg()
  assert (
    post_trade_halt_reason(
      cfg,
      starting_capital=Decimal("250"),
      realized_pnl_after=Decimal("-40"),
      trade_count_after=5,
      consecutive_losses_after=2,
      losing_trades_last_hour=1,
    )
    == "daily_loss_limit"
  )
  assert (
    post_trade_halt_reason(
      cfg,
      starting_capital=Decimal("250"),
      realized_pnl_after=Decimal("-10"),
      trade_count_after=5,
      consecutive_losses_after=4,
      losing_trades_last_hour=1,
    )
    == "max_consecutive_losses"
  )


def _signal(confidence: int = 80, setup: str = "vwap_trend") -> CandidateSignal:
  return CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type=setup,
    side="PE",
    instrument_token="1",
    tsym="P-BTC-77000-240826",
    strategy_version="t",
    confidence=confidence,
    scanner_metadata={"contract_size": "0.001"},
    feature_snapshot=FeatureSnapshot(
      ts=datetime.now(tz=timezone.utc),
      bias_5m=Bias.BEARISH,
    ),
  )


@pytest.mark.asyncio
async def test_size_entry_rejects_daily_loss():
  risk_cfg = {
    "daily_loss_limit_amount": 40,
    "max_consecutive_losses": 4,
    "max_trades_per_day": 20,
    "max_positions": 2,
    "max_premium_pct_of_available": 40,
    "max_deployed_pct_of_equity": 50,
    "default_lots": 10,
    "confidence_lot_sizing": {
      "enabled": True,
      "max_lots": 60,
      "tiers": [{"min_confidence": 80, "lots": 25}],
    },
  }
  risk = RiskEngine(SimpleNamespace(risk=risk_cfg))  # type: ignore[arg-type]
  snap = DailyRiskSnapshot(
    trade_date=date.today(),
    starting_capital=Decimal("250"),
    available_capital=Decimal("210"),
    deployed_capital=Decimal("0"),
    realized_pnl=Decimal("-40"),
    trade_count=10,
    consecutive_losses=3,
    kill_switch=False,
    entries_blocked=False,
  )
  option = OptionState(instrument_token="1", tsym="P-BTC", ltp=Decimal("500"))
  sizing = await risk.size_entry(_signal(), option, snap)
  assert sizing.approved is False
  assert sizing.rejection_reason == "daily_loss_limit"


@pytest.mark.asyncio
async def test_size_entry_rejects_flip_when_flips_disabled():
  risk_cfg = {
    "daily_loss_limit_amount": 40,
    "max_consecutive_losses": 4,
    "max_trades_per_day": 20,
    "max_positions": 2,
    "max_flips_per_day": 6,
    "max_premium_pct_of_available": 40,
    "max_deployed_pct_of_equity": 50,
    "default_lots": 10,
    "confidence_lot_sizing": {"enabled": False},
  }
  risk = RiskEngine(SimpleNamespace(risk=risk_cfg))  # type: ignore[arg-type]
  snap = DailyRiskSnapshot(
    trade_date=date.today(),
    starting_capital=Decimal("250"),
    available_capital=Decimal("250"),
    deployed_capital=Decimal("0"),
    realized_pnl=Decimal("0"),
    trade_count=0,
    consecutive_losses=0,
    kill_switch=False,
    entries_blocked=False,
    flips_disabled=True,
    flip_count=6,
  )
  option = OptionState(instrument_token="1", tsym="C-BTC", ltp=Decimal("500"))
  sizing = await risk.size_entry(
    _signal(setup="trend_reversal_flip"), option, snap, is_flip=True
  )
  assert sizing.approved is False
  assert sizing.rejection_reason in ("flips_disabled", "max_flips_per_day")
