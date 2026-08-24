"""Gap-Fix Phase 4: risk-based sizing + portfolio exposure."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from algocrypto.models.events import Bias, CandidateSignal, FeatureSnapshot, OptionState
from algocrypto.risk.engine import DailyRiskSnapshot, RiskEngine
from algocrypto.risk.portfolio import (
  PortfolioSnapshot,
  build_portfolio_snapshot,
  evaluate_portfolio_entry,
)
from algocrypto.risk.sizing import compute_lot_size, risk_based_lots


def test_risk_based_lots_caps_confidence():
  cfg = {
    "max_loss_per_trade_amount": 6,
    "expected_adverse_move_pct": 12,
  }
  lots = risk_based_lots(
    cfg,
    equity=Decimal("250"),
    entry_ltp=Decimal("500"),
    contract_size=Decimal("0.001"),
  )
  # loss/lot = 500*0.001*0.12 = 0.06; 6/0.06 = 100
  assert lots == 100


def test_compute_lot_size_binding_risk():
  cfg = {
    "default_lots": 10,
    "max_premium_pct_of_available": 40,
    "max_deployed_pct_of_equity": 50,
    "max_loss_per_trade_amount": 1.2,
    "expected_adverse_move_pct": 12,
    "confidence_lot_sizing": {
      "enabled": True,
      "max_lots": 60,
      "tiers": [{"min_confidence": 90, "lots": 40}],
    },
  }
  bd = compute_lot_size(
    cfg,
    confidence=92,
    entry_ltp=Decimal("500"),
    contract_size=Decimal("0.001"),
    available=Decimal("250"),
    deployed=Decimal("0"),
    equity=Decimal("250"),
  )
  assert bd.confidence_lots == 40
  assert bd.risk_lots == 20
  assert bd.final_lots == 20
  assert bd.binding_reason == "risk_based_limit"


def test_compute_lot_size_liquidity_binds():
  cfg = {
    "default_lots": 10,
    "max_premium_pct_of_available": 90,
    "max_deployed_pct_of_equity": 90,
    "max_loss_per_trade_amount": 0,
    "confidence_lot_sizing": {
      "enabled": True,
      "max_lots": 60,
      "tiers": [{"min_confidence": 80, "lots": 25}],
    },
  }
  bd = compute_lot_size(
    cfg,
    confidence=85,
    entry_ltp=Decimal("500"),
    contract_size=Decimal("0.001"),
    available=Decimal("250"),
    deployed=Decimal("0"),
    equity=Decimal("250"),
    liquidity_lots=10,
  )
  assert bd.final_lots == 10
  assert bd.binding_reason == "liquidity_limit"


def test_portfolio_correlated_btc_eth_same_side():
  snap = PortfolioSnapshot()
  snap.premium_by_underlying["BTC"] = Decimal("80")
  snap.premium_by_und_side["BTC:CE"] = Decimal("80")
  snap.directional_ce = Decimal("80")
  snap.combined_premium = Decimal("80")

  cfg = {
    "portfolio": {
      "max_underlying_exposure_pct": 80,
      "max_combined_exposure_pct": 80,
      "max_directional_exposure_pct": 80,
      "max_correlated_directional_pct": 40,
      "btc_eth_correlation": 0.7,
    }
  }
  d = evaluate_portfolio_entry(
    cfg,
    snapshot=snap,
    equity=Decimal("250"),
    new_underlying="ETH",
    new_side="CE",
    new_premium=Decimal("40"),
  )
  assert d.allow is True

  d2 = evaluate_portfolio_entry(
    cfg,
    snapshot=snap,
    equity=Decimal("250"),
    new_underlying="ETH",
    new_side="CE",
    new_premium=Decimal("50"),
  )
  assert d2.allow is False
  assert d2.reason == "portfolio_correlated_exposure_limit"


def test_portfolio_opposite_side_not_correlated_gate():
  snap = PortfolioSnapshot()
  snap.premium_by_underlying["BTC"] = Decimal("80")
  snap.premium_by_und_side["BTC:CE"] = Decimal("80")
  snap.directional_ce = Decimal("80")
  snap.combined_premium = Decimal("80")
  cfg = {
    "portfolio": {
      "max_underlying_exposure_pct": 80,
      "max_combined_exposure_pct": 80,
      "max_directional_exposure_pct": 80,
      "max_correlated_directional_pct": 30,
      "btc_eth_correlation": 0.7,
    }
  }
  d = evaluate_portfolio_entry(
    cfg,
    snapshot=snap,
    equity=Decimal("250"),
    new_underlying="ETH",
    new_side="PE",
    new_premium=Decimal("50"),
  )
  assert d.allow is True


def test_build_portfolio_from_positions():
  pos = SimpleNamespace(
    tsym="C-BTC-77000-240826",
    option_side="CE",
    premium_deployed=Decimal("25"),
    quantity=10,
    signal_snapshot={"underlying": "BTC", "strike_pick": {"delta": 0.5}},
  )
  snap = build_portfolio_snapshot([pos])
  assert snap.premium_by_underlying["BTC"] == Decimal("25")
  assert snap.premium_by_und_side["BTC:CE"] == Decimal("25")
  assert snap.net_delta == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_size_entry_applies_risk_and_portfolio():
  risk_cfg = {
    "max_premium_pct_of_available": 40,
    "max_deployed_pct_of_equity": 50,
    "max_loss_per_trade_amount": 1.2,
    "expected_adverse_move_pct": 12,
    "daily_loss_limit_amount": 0,
    "max_consecutive_losses": 0,
    "max_trades_per_day": 0,
    "max_positions": 0,
    "max_flips_per_day": 0,
    "default_lots": 10,
    "confidence_lot_sizing": {
      "enabled": True,
      "max_lots": 60,
      "tiers": [{"min_confidence": 90, "lots": 40}],
    },
    "portfolio": {
      "max_underlying_exposure_pct": 35,
      "max_combined_exposure_pct": 50,
      "max_directional_exposure_pct": 40,
      "max_correlated_directional_pct": 45,
      "btc_eth_correlation": 0.7,
    },
  }
  config = SimpleNamespace(risk=risk_cfg, position_exit={"adverse_move_pct_from_entry": 12})
  risk = RiskEngine(config)  # type: ignore[arg-type]
  signal = CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="vwap_trend",
    side="CE",
    instrument_token="1",
    tsym="C-BTC-77000-240826",
    strategy_version="t",
    confidence=92,
    scanner_metadata={"contract_size": "0.001", "underlying": "BTC"},
    feature_snapshot=FeatureSnapshot(
      ts=datetime.now(tz=timezone.utc), bias_5m=Bias.BULLISH
    ),
  )
  option = OptionState(instrument_token="1", tsym=signal.tsym, ltp=Decimal("500"))
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
  assert sizing.lots == 20
  assert sizing.binding_reason == "risk_based_limit"
  assert sizing.size_breakdown is not None
