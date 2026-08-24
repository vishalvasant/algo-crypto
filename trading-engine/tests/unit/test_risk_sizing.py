"""Unit tests for confidence-based lot sizing (Delta crypto lots)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from algocrypto.models.events import Bias, CandidateSignal, FeatureSnapshot, OptionState
from algocrypto.risk.engine import (
  DailyRiskSnapshot,
  RiskEngine,
  fit_lots_to_capital,
  lots_for_confidence,
)


def _signal(confidence: int) -> CandidateSignal:
  return CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="vwap_trend",
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


def _risk_cfg(**overrides):
  cfg = {
    "default_lots": 1,
    "max_premium_pct_of_available": 65,
    "max_deployed_pct_of_equity": 85,
    "daily_loss_limit_amount": 0,
    "daily_loss_limit_pct": 0,
    "max_daily_loss": 0,
    "max_trades_per_day": 0,
    "max_concurrent_positions": 0,
    "max_positions": 0,
    "max_consecutive_losses": 0,
    "max_flips_per_day": 0,
    "max_losing_trades_per_hour": 0,
    "confidence_lot_sizing": {
      "enabled": True,
      "max_lots": 3,
      "tiers": [
        {"min_confidence": 70, "lots": 1},
        {"min_confidence": 80, "lots": 2},
        {"min_confidence": 90, "lots": 3},
      ],
    },
  }
  cfg.update(overrides)
  return cfg


def test_lots_for_confidence_tiers():
  cfg = _risk_cfg()
  assert lots_for_confidence(cfg, 69) == 1
  assert lots_for_confidence(cfg, 70) == 1
  assert lots_for_confidence(cfg, 79) == 1
  assert lots_for_confidence(cfg, 80) == 2
  assert lots_for_confidence(cfg, 89) == 2
  assert lots_for_confidence(cfg, 90) == 3
  assert lots_for_confidence(cfg, 100) == 3


def test_lots_for_confidence_disabled_uses_default():
  cfg = _risk_cfg()
  cfg["confidence_lot_sizing"]["enabled"] = False
  cfg["default_lots"] = 2
  assert lots_for_confidence(cfg, 95) == 2


def test_lots_capped_by_max_lots():
  cfg = _risk_cfg()
  cfg["confidence_lot_sizing"]["max_lots"] = 2
  assert lots_for_confidence(cfg, 95) == 2


@pytest.mark.asyncio
async def test_size_entry_uses_confidence_lots():
  """qty = Delta lots; premium = price × lots × contract_size."""
  config = SimpleNamespace(risk=_risk_cfg())
  risk = RiskEngine(config)  # type: ignore[arg-type]

  signal = _signal(92)
  # 3 lots × $500 × 0.001 = $1.50
  option = OptionState(instrument_token="1", tsym="P-BTC", ltp=Decimal("500"))
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
  )

  sizing = await risk.size_entry(signal, option, snap)
  assert sizing.approved
  assert sizing.lots == 3
  assert sizing.quantity == 3
  assert sizing.confidence == 92
  assert sizing.premium_required == Decimal("1.500")


@pytest.mark.asyncio
async def test_size_entry_steps_down_when_capital_tight():
  """High premium + tight % cap forces fewer lots."""
  config = SimpleNamespace(
    risk=_risk_cfg(
      max_premium_pct_of_available=10,
      confidence_lot_sizing={
        "enabled": True,
        "max_lots": 100,
        "tiers": [{"min_confidence": 90, "lots": 100}],
      },
    )
  )
  risk = RiskEngine(config)  # type: ignore[arg-type]

  signal = _signal(95)
  # 100 lots × $800 × 0.001 = $80 > 10% of $250 (= $25) → must step down
  option = OptionState(instrument_token="1", tsym="P-BTC", ltp=Decimal("800"))
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
  )

  sizing = await risk.size_entry(signal, option, snap)
  assert sizing.approved
  # max affordable: floor(25 / 0.8) = 31 lots
  assert sizing.lots <= 31
  assert sizing.lots >= 1
  assert sizing.premium_required <= Decimal("25")


def test_fit_lots_fills_up_toward_deploy_when_conf_high():
  """Tier say 2 lots at conf 87, but room allows 3 → fill to 3."""
  cfg = _risk_cfg()
  lots, prem = fit_lots_to_capital(
    cfg,
    confidence=87,
    entry_ltp=Decimal("500"),
    contract_size=Decimal("0.001"),
    available=Decimal("250"),
    deployed=Decimal("0"),
    equity=Decimal("250"),
  )
  assert lots == 3
  assert prem == Decimal("1.500")


@pytest.mark.asyncio
async def test_loss_stops_disabled_when_zero():
  """Zero limits must not block entries."""
  config = SimpleNamespace(
    risk=_risk_cfg(max_consecutive_losses=0, daily_loss_limit_amount=0)
  )
  risk = RiskEngine(config)  # type: ignore[arg-type]
  signal = _signal(80)
  option = OptionState(instrument_token="1", tsym="P-BTC", ltp=Decimal("500"))
  snap = DailyRiskSnapshot(
    trade_date=date.today(),
    starting_capital=Decimal("250"),
    available_capital=Decimal("100"),
    deployed_capital=Decimal("0"),
    realized_pnl=Decimal("-150"),
    trade_count=12,
    consecutive_losses=20,
    kill_switch=False,
    entries_blocked=False,
  )
  sizing = await risk.size_entry(signal, option, snap)
  assert sizing.approved
  assert sizing.rejection_reason is None
