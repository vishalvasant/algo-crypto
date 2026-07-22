"""Institutional strategy scanners driven by features.extra strategy_setups."""
from __future__ import annotations

from datetime import datetime, timezone

import structlog

from algocrypto.config import AppConfig
from algocrypto.contract_selector.resolve import resolve_side_contract
from algocrypto.contract_selector.selector import ContractUniverse
from algocrypto.models.events import Bias, CandidateSignal, FeatureSnapshot, OptionState

logger = structlog.get_logger(__name__)

STRATEGY_NAMES: list[str] = [
  "vwap_reclaim",
  "vwap_bounce",
  "vwap_pullback",
  "vwap_rejection",
  "ema_pullback",
  "opening_range_breakout",
  "momentum_continuation",
  "trend_continuation",
  "vwap_trend",
  "reversal",
  "cpr_breakout",
  "pdh_pdl_break",
  "oi_breakout",
  "delta_momentum",
  "gamma_expansion",
  "iv_expansion",
  "gap_and_go",
  "trend_day",
  "mean_reversion",
  "liquidity_sweep",
  "expiry_scalping",
]

_BIAS_EXEMPT = frozenset({"mean_reversion", "liquidity_sweep", "reversal"})


def _as_states(
  option_states: dict[str, OptionState | None] | OptionState | None,
) -> dict[str, OptionState | None]:
  if option_states is None:
    return {}
  if isinstance(option_states, dict):
    return option_states
  return {option_states.instrument_token: option_states}


def _emit(
  config: AppConfig,
  name: str,
  side: str,
  features: FeatureSnapshot,
  universe: ContractUniverse,
  option_states: dict[str, OptionState | None] | OptionState | None,
) -> CandidateSignal | None:
  if features.nifty_spot is None:
    return None

  states = _as_states(option_states)
  expiry = None
  for inst0 in (universe.atm_ce, universe.atm_pe):
    if inst0 is not None and inst0.expiry_date is not None:
      expiry = inst0.expiry_date.date()
      break
  resolved = resolve_side_contract(
    config=config,
    universe=universe,
    side=side,
    spot=features.nifty_spot,
    option_states=states,
    expiry=expiry,
    now=features.ts,
  )
  if resolved is None:
    return None
  inst, option_state, pick = resolved

  extra = features.extra or {}
  setup_label = extra.get("strategy_setups", {}).get(name)

  signal = CandidateSignal(
    ts=features.ts or datetime.now(tz=timezone.utc),
    setup_type=name,
    side=side,
    instrument_token=inst.token,
    tsym=inst.tsym,
    strategy_version=config.strategy.get(
      "strategy_version",
      "strategy_router_v1.2.0_institutional_lite",
    ),
    feature_snapshot=features,
    scanner_metadata={
      "atm_strike": str(universe.atm_strike),
      "option_ltp": str(option_state.ltp),
      "exchange": inst.exchange,
      "lot_size": 1,
      "contract_size": str(getattr(inst, "contract_size", "0.001")),
      "underlying": getattr(inst, "underlying", "BTC"),
      "setup": setup_label,
      "strike_pick": pick,
    },
  )
  logger.info(
    "candidate_signal",
    setup=signal.setup_type,
    side=side,
    tsym=inst.tsym,
    strike=str(inst.strike),
    ltp=str(option_state.ltp),
    delta=pick.get("delta"),
  )
  return signal


def _vwap_reclaim_aligned(features: FeatureSnapshot, setup: str) -> bool:
  if not features.setup_3m or not features.trigger_1m:
    return False
  if setup == "bull":
    return (
      features.bias_5m == Bias.BULLISH
      and features.setup_3m == "vwap_reclaim_bull"
      and features.trigger_1m == "vwap_reclaim_cross_up"
    )
  if setup == "bear":
    return (
      features.bias_5m == Bias.BEARISH
      and features.setup_3m == "vwap_reclaim_bear"
      and features.trigger_1m == "vwap_reclaim_cross_down"
    )
  return False


class FeatureSetupScanner:
  """Generic scanner: strategy_setups[name] bull/bear → CE/PE contract."""

  def __init__(self, config: AppConfig, name: str) -> None:
    self._config = config
    self.name = name

  def scan(
    self,
    features: FeatureSnapshot,
    universe: ContractUniverse,
    option_states: dict[str, OptionState | None] | OptionState | None,
  ) -> CandidateSignal | None:
    if features.session_vwap is None or features.nifty_spot is None:
      return None

    extra = features.extra or {}
    setup = extra.get("strategy_setups", {}).get(self.name)
    if setup not in ("bull", "bear"):
      return None

    if self.name not in _BIAS_EXEMPT and features.bias_5m == Bias.NEUTRAL:
      return None

    if self.name not in _BIAS_EXEMPT:
      if setup == "bull" and features.bias_5m != Bias.BULLISH:
        return None
      if setup == "bear" and features.bias_5m != Bias.BEARISH:
        return None

    if self.name == "vwap_reclaim" and not _vwap_reclaim_aligned(features, setup):
      return None

    # mean_reversion: bull = fade down → CE, bear = fade up → PE
    side = "CE" if setup == "bull" else "PE"
    return _emit(self._config, self.name, side, features, universe, option_states)


def build_strategy_scanners(config: AppConfig) -> list[FeatureSetupScanner]:
  router_cfg = config.strategy.get("router", {})
  enabled: list[str] = router_cfg.get("enabled_strategies") or []
  if not enabled:
    enabled = list(STRATEGY_NAMES)

  return [FeatureSetupScanner(config, name) for name in enabled if name in STRATEGY_NAMES]


build_all_scanners = build_strategy_scanners
