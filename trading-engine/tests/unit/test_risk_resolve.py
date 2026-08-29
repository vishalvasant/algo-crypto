from algocrypto.risk.circuit_breakers import evaluate_circuit_breakers, CircuitSnapshot
from algocrypto.risk.resolve import resolve_risk_config
from algocrypto.risk.states import RiskState
from decimal import Decimal


def test_resolve_risk_config_disables_circuit_breakers_in_paper():
  cfg = {
    "max_consecutive_losses": 4,
    "daily_loss_limit_amount": 40,
    "paper": {"enabled": True, "disable_circuit_breakers": True},
  }
  out = resolve_risk_config(cfg, is_paper=True)
  assert out["max_consecutive_losses"] == 0
  assert out["daily_loss_limit_amount"] == 0
  assert out["paper_disable_circuit_breakers"] is True


def test_resolve_risk_config_unchanged_in_live():
  cfg = {"max_consecutive_losses": 4, "paper": {"enabled": True}}
  out = resolve_risk_config(cfg, is_paper=False)
  assert out["max_consecutive_losses"] == 4


def test_paper_resolved_config_allows_entry_after_consecutive_losses():
  base = {
    "max_consecutive_losses": 4,
    "paper": {"enabled": True, "disable_circuit_breakers": True},
  }
  risk = resolve_risk_config(base, is_paper=True)
  snap = CircuitSnapshot(
    starting_capital=Decimal("250"),
    realized_pnl=Decimal("-10"),
    trade_count=4,
    consecutive_losses=4,
    open_position_count=0,
    flip_count=0,
    flips_last_hour=0,
    losing_trades_last_hour=4,
    kill_switch=False,
    entries_blocked=True,
    risk_state=RiskState.HALTED,
  )
  decision = evaluate_circuit_breakers(risk, snap)
  assert decision.allow_entry is True
