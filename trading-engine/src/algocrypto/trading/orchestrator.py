from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog

from pathlib import Path

from algocrypto.config import AppConfig
from algocrypto.contract_selector.expiry import parse_expiry_tag
from algocrypto.contract_selector.resolve import resolve_side_contract
from algocrypto.contract_selector.selector import ContractSelector, ContractUniverse
from algocrypto.contract_selector.strike_picker import atm_band_instruments
from algocrypto.execution.engine import ExecutionEngine
from algocrypto.features.chain_intel import build_chain_snapshot
from algocrypto.features.engine import FeatureEngine
from algocrypto.journal.analytics import StrategyLearner
from algocrypto.journal.writer import JournalWriter
from algocrypto.market_data.engine import MarketDataEngine
from algocrypto.models.events import (
  Bias,
  CandidateSignal,
  CandleInterval,
  MarketRegime,
  OptionState,
  QuoteUpdate,
  SystemEvent,
)
from algocrypto.option_data.greeks import compute_greeks
from algocrypto.option_data.layer import OptionDataLayer
from algocrypto.option_data.vol_model import (
  IVHistory,
  build_vol_snapshot,
  tte_entry_adjustments,
)
from algocrypto.position.manager import PositionManager
from algocrypto.position.thesis import build_thesis_from_signal
from algocrypto.quality.gate import QualityGate
from algocrypto.regime.classifier import RegimeClassifier
from algocrypto.risk.engine import RiskEngine
from algocrypto.safety.engine import SafetyEngine
from algocrypto.scanner.library import build_strategy_scanners
from algocrypto.strategy.families import strategy_family
from algocrypto.strategy.router import StrategyRouter
from algocrypto.trading.ev_engine import estimate_ev, resolve_ev_engine_config
from algocrypto.validator.engine import RuleValidator

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class TradingOrchestrator:
  def __init__(
    self,
    config: AppConfig,
    broker,
    journal: JournalWriter,
    market_data: MarketDataEngine,
    option_data: OptionDataLayer,
  ) -> None:
    self._config = config
    self._broker = broker
    self._journal = journal
    self._market_data = market_data
    self._option_data = option_data
    self._universe: ContractUniverse | None = None
    self._prior_oi: dict[str, int] = {}

    self.features = FeatureEngine(config, market_data)
    self.regime = RegimeClassifier(config)
    self.quality = QualityGate(config)
    root = Path(__file__).resolve().parents[4]
    self.learner = StrategyLearner(root / "reports" / "strategy_learner.json")
    self.quality.set_learner(self.learner)
    scanners = build_strategy_scanners(config)
    # Keep attribute for debug endpoints that still expect .scanner
    self.scanner = next((s for s in scanners if s.name == "vwap_reclaim"), scanners[0])
    self.router = StrategyRouter(config, scanners, self.quality)
    self.validator = RuleValidator(config)
    self.risk = RiskEngine(config)
    self.safety = SafetyEngine(config)
    self.execution = ExecutionEngine(config, broker, journal)
    self.positions = PositionManager(
      config, broker, journal, self.risk, market_data
    )
    self.positions.set_trade_close_hook(self._on_trade_closed)
    self._contract_selector = ContractSelector(config, broker)
    self._last_flip_at: datetime | None = None
    self._last_entry_block: str | None = None

    self._scan_lock = asyncio.Lock()
    self._entering_tokens: set[str] = set()
    self._last_scan_at: datetime | None = None
    self._scan_interval_sec = int(config.runtime.get("scan_interval_seconds", 10))
    self._log_every = bool(config.strategy.get("router", {}).get("log_every_decision", True))
    self._running = False
    self._iv_history = IVHistory(maxlen=120)
    self._pending_thesis = None

  def _on_trade_closed(self, setup_type: str, pnl: Decimal, exit_reason: str, pos=None) -> None:
    regime = None
    underlying = None
    expiry_bucket = None
    mfe = mae = None
    if pos is not None:
      snap = pos.signal_snapshot or {}
      extra = snap.get("extra") or {}
      regime = extra.get("regime_primary") or getattr(self.positions, "_regime_primary", None)
      expiry_bucket = extra.get("expiry_bucket")
      from algocrypto.symbols_util import underlying_from_tsym

      underlying = underlying_from_tsym(pos.tsym)
      mfe = float(pos.mfe) if pos.mfe is not None else None
      mae = float(pos.mae) if pos.mae is not None else None
    self.learner.record_trade(
      setup_type,
      pnl,
      exit_reason=exit_reason,
      regime=regime,
      underlying=underlying,
      expiry_bucket=expiry_bucket,
      mfe=mfe,
      mae=mae,
    )

  def set_universe(self, universe: ContractUniverse) -> None:
    self._universe = universe

  @property
  def last_entry_block(self) -> str | None:
    return self._last_entry_block

  async def initialize(self) -> None:
    await self.risk.clear_paper_circuit_halt()
    snap = await self.risk.ensure_daily_state()
    await self._load_prior_day_levels()
    logger.info(
      "paper_account_initialized",
      capital=str(snap.available_capital),
      starting=str(snap.starting_capital),
      strategies=len(self.router._scanners),
    )

  async def _load_prior_day_levels(self) -> None:
    """PDH/PDL/CPR inputs from previous UTC calendar day (crypto 24×7)."""
    try:
      from datetime import time, timedelta

      from algocrypto.symbols_util import index_symbol

      today = datetime.now(IST).date()
      spot_token = index_symbol(self._config.symbols)
      exchange = self._config.symbols.get("exchange_spot", "DELTA")
      for back in range(1, 6):
        d = today - timedelta(days=back)
        start = datetime.combine(d, time(0, 0), tzinfo=IST).astimezone(timezone.utc)
        end = datetime.combine(d, time(23, 59), tzinfo=IST).astimezone(timezone.utc)
        rows = await self._broker.get_candles(
          exchange,
          spot_token,
          CandleInterval.M5,
          start,
          end,
        )
        if rows:
          hi = max(c.high for c in rows)
          lo = min(c.low for c in rows)
          cl = rows[-1].close
          self.features.set_prior_day(hi, lo, cl)
          logger.info("prior_day_levels", date=str(d), pdh=str(hi), pdl=str(lo), close=str(cl))
          return
    except Exception:
      logger.exception("prior_day_levels_failed")

  async def run_periodic_scan(self) -> None:
    self._running = True
    while self._running:
      try:
        if self._market_open():
          # Must share _scan_lock with on_quote — otherwise two concurrent
          # scans can both pass has_open_for_token and double-enter the same
          # contract (seen live: identical entry+exit timestamps).
          async with self._scan_lock:
            await self._scan_for_entry()
      except asyncio.CancelledError:
        raise
      except Exception:
        logger.exception("periodic_scan_failed")
      await asyncio.sleep(self._scan_interval_sec)

  def stop(self) -> None:
    self._running = False

  async def on_quote(self, quote: QuoteUpdate) -> None:
    await self.positions.on_quote(quote)

    if self._universe is None or not self._market_open():
      return

    # Flips + entry scans share one lock so they cannot race a periodic scan
    # into a duplicate same-token fill.
    async with self._scan_lock:
      if self.positions._pending_flips:
        flips = self.positions.pop_pending_flips()
        for flip in flips:
          try:
            await self._try_reversal_flip(flip)
          except Exception:
            logger.exception("reversal_flip_failed", flip=flip)

      now = datetime.now(tz=timezone.utc)
      if (
        self._last_scan_at
        and (now - self._last_scan_at).total_seconds() < self._scan_interval_sec
      ):
        return
      self._last_scan_at = now
      await self._scan_for_entry()

  def _band_option_states(self, universe: ContractUniverse) -> dict[str, OptionState | None]:
    """LTP map for ATM±N CE/PE — used by Greek strike picker."""
    from algocrypto.symbols_util import strike_step

    under = universe.underlying if universe else None
    step = Decimal(str(strike_step(self._config.symbols, under)))
    band = int(
      (self._config.strategy.get("strike_selection") or {}).get(
        "atm_band_steps",
        self._config.symbols.get("atm_band_steps", 1),
      )
    )
    states: dict[str, OptionState | None] = {}
    for side in ("CE", "PE"):
      for inst in atm_band_instruments(universe, side, band_steps=band, step=step):
        states[inst.token] = self._option_data.get(inst.token)
    if universe.atm_ce:
      states.setdefault(
        universe.atm_ce.token, self._option_data.get(universe.atm_ce.token)
      )
    if universe.atm_pe:
      states.setdefault(
        universe.atm_pe.token, self._option_data.get(universe.atm_pe.token)
      )
    return states

  def _refresh_chain_and_option_context(
    self,
    universe: ContractUniverse,
    band_states: dict[str, OptionState | None],
    spot: Decimal | None,
  ) -> None:
    """OI/PCR/MaxPain + ATM option VWAP/Greeks into FeatureEngine."""
    chain = build_chain_snapshot(universe, band_states, prior_oi=self._prior_oi)
    self.features.set_chain_snapshot(chain)
    # Refresh prior OI baseline for next delta
    for tok, st in band_states.items():
      if st is not None and st.oi is not None:
        self._prior_oi[tok] = int(st.oi)

    # Prefer ATM side matching live bias for option VWAP / greeks context
    atm = universe.atm_ce or universe.atm_pe
    bias_spot = spot
    vwap = self._market_data.session_vwap_value
    prefer_pe = (
      bias_spot is not None and vwap is not None and bias_spot < vwap
    )
    inst = universe.atm_pe if prefer_pe and universe.atm_pe else universe.atm_ce
    if inst is None:
      inst = atm
    ctx: dict = {}
    if inst is not None:
      st = band_states.get(inst.token) or self._option_data.get(inst.token)
      if st is not None:
        ctx["ltp"] = float(st.ltp) if st.ltp is not None else None
        ctx["spread_pct"] = float(st.spread_pct) if st.spread_pct is not None else None
        ctx["volume"] = st.volume
        ctx["oi"] = st.oi
        ctx["option_vwap"] = self._option_data.option_vwap(inst.token)
        if spot is not None and st.ltp is not None and inst.expiry_date is not None:
          g = compute_greeks(
            spot=float(spot),
            strike=float(inst.strike),
            premium=float(st.ltp),
            option_type=inst.option_type,
            expiry=inst.expiry_date.date(),
          )
          ctx["delta"] = g.delta
          ctx["gamma"] = g.gamma
          ctx["iv"] = g.iv
          ctx["theta"] = g.theta
          ctx["vega"] = g.vega
          # Phase 5: IV/RV/TTE snapshot
          vol_cfg = self._config.strategy.get("vol_model") or {}
          m1 = self._market_data.candles(CandleInterval.M1)
          snap = build_vol_snapshot(
            iv=g.iv,
            history=self._iv_history,
            candles=m1,
            spot=float(spot) if spot is not None else None,
            expiry=inst.expiry_date.date() if inst.expiry_date else None,
            cfg=vol_cfg,
          )
          ctx["iv_change"] = snap.iv_change
          ctx["iv_rank"] = snap.iv_rank
          ctx["iv_percentile"] = snap.iv_percentile
          ctx["iv_regime"] = snap.iv_regime.value
          ctx["realized_vol"] = snap.realized_vol
          ctx["iv_vs_rv"] = snap.iv_vs_rv
          ctx["expected_move"] = snap.expected_move
          ctx["time_to_expiry_minutes"] = snap.time_to_expiry_minutes
          ctx["expiry_bucket"] = snap.expiry_bucket.value
          ctx["vol_unavailable"] = list(snap.unavailable)
    self.features.set_option_context(ctx)

  async def _scan_for_entry(self) -> None:
    if self._universe is None:
      return

    refreshed = await self._market_data.refresh_session_candles()
    if refreshed:
      for interval_candles in self._market_data._candles.values():
        await self._journal.write_candles(interval_candles)

    # Safety layer — data freshness / broker (Gap-Fix Phase 1.2)
    safety = self.safety.evaluate(
      last_spot_quote_ts=self._market_data.last_quote_ts
      or self._market_data.latest_candle_ts(),
      last_option_quote_ts=self._market_data.last_quote_ts,
      last_candle_ts=self._market_data.latest_candle_ts(),
      candles_stale=self._market_data.candles_stale(),
      broker_connected=True,
      db_ok=True,
    )
    if not safety.allow_entries:
      if self._log_every:
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=datetime.now(tz=timezone.utc),
            severity="warning",
            message="NO_TRADE",
            metadata={
              "selected_strategy": "NO_TRADE",
              "selected_reason": "safety_" + (safety.reasons[0] if safety.reasons else "halted"),
              "trade_allowed": False,
              "safety_state": safety.state.value,
              "safety_reasons": safety.reasons,
              "safety_details": safety.details,
            },
          )
        )
      return

    # Stale candle feed → do not trade on outdated VWAP/setups.
    # A failed refresh alone is OK if we still have a fresh last bar.
    if self._market_data.candles_stale():
      if self._log_every:
        latest = self._market_data.latest_candle_ts()
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=datetime.now(tz=timezone.utc),
            severity="warning",
            message="NO_TRADE",
            metadata={
              "selected_strategy": "NO_TRADE",
              "selected_reason": "stale_candle_feed",
              "trade_allowed": False,
              "confidence": 0,
              "last_m1_ts": latest.isoformat() if latest else None,
              "last_refresh_ok": self._market_data.last_refresh_ok,
            },
          )
        )
      return

    features = self.features.compute()
    m1 = self._market_data.candles(CandleInterval.M1)
    m5 = self._market_data.candles(CandleInterval.M5)
    is_expiry = self._is_expiry_day()
    self.features.is_expiry_day = is_expiry
    regime = self.regime.classify(
      features, m1, m5, is_expiry_day=is_expiry
    )
    self.positions.set_regime(regime.primary)

    # Retarget ATM from live spot so concurrent books can use moved strikes
    # (same behaviour as day_backtest bar-by-bar ATM rounding).
    spot = features.nifty_spot or self._market_data.spot_ltp
    universe = self._universe
    if spot is not None:
      universe = self._contract_selector.retarget_atm(self._universe, spot)
      self._universe = universe

    # ATM±1 (and ATM) LTP map for Greek strike selection on both sides.
    band_states = self._band_option_states(universe)
    self._refresh_chain_and_option_context(universe, band_states, spot)
    # Recompute features with chain + option Greeks context.
    features = self.features.compute()
    ce_state = (
      band_states.get(universe.atm_ce.token) if universe.atm_ce else None
    )
    pe_state = (
      band_states.get(universe.atm_pe.token) if universe.atm_pe else None
    )

    options_by_strategy = {s.name: band_states for s in self.router._scanners}

    decision, signal = self.router.route(
      features, regime, universe, options_by_strategy
    )

    # Persist every scan decision so the Decision Logs page stays current.
    if self._log_every:
      meta = decision.model_dump(mode="json")
      meta["scan_interval_seconds"] = self._scan_interval_sec
      meta["ce_ltp"] = float(ce_state.ltp) if ce_state and ce_state.ltp is not None else None
      meta["pe_ltp"] = float(pe_state.ltp) if pe_state and pe_state.ltp is not None else None
      await self._journal.write_system_event(
        SystemEvent(
          event_type="strategy_decision",
          ts=decision.ts,
          severity="info" if decision.trade_allowed else "debug",
          message=decision.selected_reason or decision.selected_strategy,
          metadata=meta,
        )
      )

    if signal is None:
      logger.debug(
        "scan_no_trade",
        strategy=decision.selected_strategy,
        reason=decision.selected_reason,
        bias=features.bias_5m.value,
        regime=regime.primary,
        setup=features.setup_3m,
        trigger=features.trigger_1m,
        pullback=(features.extra or {}).get("setup_vwap_pullback"),
        pe_ltp=str(pe_state.ltp) if pe_state and pe_state.ltp is not None else None,
        ce_ltp=str(ce_state.ltp) if ce_state and ce_state.ltp is not None else None,
      )
      return

    from algocrypto.features.crypto_scaling import crypto_entry_allowed

    allowed, gate_reason = crypto_entry_allowed(
      self._config.strategy, features, regime, signal
    )
    if not allowed:
      logger.info(
        "entry_skipped_crypto_gate",
        setup=signal.setup_type,
        side=signal.side,
        reason=gate_reason,
      )
      return

    await self._execute_signal(signal, is_expiry=is_expiry)

  async def _try_reversal_flip(self, flip: dict) -> None:
    """After trend_reversal exit, evaluate opposite side — not a guaranteed trade.

    Gap-Fix Phase 2: confirmation + cooldown + quality gate; no forced confidence=80.
    """
    from algocrypto.position.reversal_confirm import confirm_reversal

    if self._universe is None:
      return
    side = flip.get("side")
    if side not in ("CE", "PE"):
      return

    exit_cfg = self._config.position_exit
    now = datetime.now(tz=timezone.utc)
    cooldown = int(exit_cfg.get("flip_cooldown_seconds", 300))
    if (
      cooldown > 0
      and self._last_flip_at is not None
      and (now - self._last_flip_at).total_seconds() < cooldown
    ):
      logger.info(
        "reversal_flip_blocked",
        reason="flip_cooldown",
        cooldown_seconds=cooldown,
        from_side=flip.get("from_side"),
        to_side=side,
      )
      await self._journal.write_system_event(
        SystemEvent(
          event_type="strategy_decision",
          ts=now,
          severity="warning",
          message="NO_TRADE",
          metadata={
            "selected_strategy": "NO_TRADE",
            "selected_reason": "flip_cooldown",
            "trade_allowed": False,
            "setup_type": "trend_reversal_flip",
            "cooldown_seconds": cooldown,
          },
        )
      )
      return

    risk_snap = await self.risk.ensure_daily_state()
    gate = self.risk.evaluate_gates(
      risk_snap,
      open_position_count=self.positions.open_count,
      is_flip=True,
    )
    if not gate.allow_entry or not gate.allow_flip:
      logger.info(
        "reversal_flip_blocked",
        reason=gate.reason or "flips_disabled",
        state=gate.state.value,
        from_side=flip.get("from_side"),
        to_side=side,
      )
      await self._journal.write_system_event(
        SystemEvent(
          event_type="strategy_decision",
          ts=now,
          severity="warning",
          message="NO_TRADE",
          metadata={
            "selected_strategy": "NO_TRADE",
            "selected_reason": gate.reason or "flips_disabled",
            "trade_allowed": False,
            "setup_type": "trend_reversal_flip",
            "risk_state": gate.state.value,
          },
        )
      )
      return

    features = self.features.compute()
    spot = features.nifty_spot or self._market_data.spot_ltp
    vwap = features.session_vwap or self._market_data.session_vwap_value
    m1 = self._market_data.candles(CandleInterval.M1)
    m5 = self._market_data.candles(CandleInterval.M5)

    if bool(exit_cfg.get("flip_requires_confirmation", True)):
      conf = confirm_reversal(
        new_side=side,
        spot=spot,
        vwap=vwap,
        m1=m1,
        m5=m5,
        cfg=exit_cfg,
      )
      await self._journal.write_system_event(
        SystemEvent(
          event_type="reversal_confirmation",
          ts=now,
          severity="info" if conf.confirmed else "debug",
          message="confirmed" if conf.confirmed else "rejected",
          metadata={
            "from_side": flip.get("from_side"),
            "to_side": side,
            "confirmed": conf.confirmed,
            "score": conf.score,
            "reasons": list(conf.reasons),
            "blockers": list(conf.blockers),
            "atr": str(conf.atr) if conf.atr is not None else None,
            "distance_to_vwap": (
              str(conf.distance_to_vwap) if conf.distance_to_vwap is not None else None
            ),
            "required_distance": (
              str(conf.required_distance)
              if conf.required_distance is not None
              else None
            ),
          },
        )
      )
      if not conf.confirmed:
        logger.info(
          "reversal_flip_not_confirmed",
          score=conf.score,
          blockers=list(conf.blockers),
          to_side=side,
        )
        return

    if spot is None:
      return
    universe = self._contract_selector.retarget_atm(self._universe, spot)
    self._universe = universe
    band_states = self._band_option_states(universe)
    resolved = resolve_side_contract(
      config=self._config,
      universe=universe,
      side=side,
      spot=spot,
      option_states=band_states,
    )
    if resolved is None:
      logger.info("reversal_flip_no_contract", side=side)
      return
    inst, opt_state, pick = resolved

    bias = Bias.BULLISH if side == "CE" else Bias.BEARISH
    feat = features.model_copy(update={"bias_5m": bias})
    signal = CandidateSignal(
      ts=now,
      setup_type="trend_reversal_flip",
      side=side,
      instrument_token=inst.token,
      tsym=inst.tsym,
      strategy_version=self._config.strategy.get(
        "strategy_version", "strategy_router_v1.1.0"
      ),
      feature_snapshot=feat,
      confidence=0,
      scanner_metadata={
        "atm_strike": str(universe.atm_strike),
        "option_ltp": str(opt_state.ltp),
        "exchange": inst.exchange,
        "lot_size": 1,
        "contract_size": str(getattr(inst, "contract_size", "0.001")),
        "underlying": getattr(inst, "underlying", "BTC"),
        "flip_from": flip.get("from_side"),
        "strike_pick": pick,
      },
    )

    # Score via QualityGate — never inject fake confidence=80.
    if bool(exit_cfg.get("flip_use_quality_gate", True)):
      m1_c = m1
      m5_c = m5
      regime = self.regime.classify(feat, m1_c, m5_c, is_expiry_day=self._is_expiry_day())
      ctx = {
        "ltp": float(opt_state.ltp) if opt_state.ltp is not None else None,
        "spread_pct": float(opt_state.spread_pct) if opt_state.spread_pct is not None else None,
        "delta": pick.get("delta"),
        "gamma": pick.get("gamma"),
        "iv": pick.get("iv"),
      }
      rule_score, conf_logs = self.quality.score(signal, feat, regime, context=ctx)
      signal.confidence = rule_score
      signal.scanner_metadata["confidence"] = rule_score
      signal.scanner_metadata["rule_score"] = rule_score
      signal.scanner_metadata["confidence_logs"] = conf_logs
      min_score = int(exit_cfg.get("flip_min_rule_score", self.quality.min_confidence))
      if rule_score < min_score:
        logger.info(
          "reversal_flip_low_score",
          rule_score=rule_score,
          min_score=min_score,
          tsym=inst.tsym,
        )
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=now,
            severity="debug",
            message="NO_TRADE",
            metadata={
              "selected_strategy": "NO_TRADE",
              "selected_reason": f"flip_rule_score={rule_score} < {min_score}",
              "trade_allowed": False,
              "setup_type": "trend_reversal_flip",
              "rule_score": rule_score,
            },
          )
        )
        return
    else:
      signal.confidence = int(exit_cfg.get("flip_min_rule_score", 75))

    logger.info(
      "reversal_flip_entry_attempt",
      from_side=flip.get("from_side"),
      to_side=side,
      tsym=inst.tsym,
      strike=str(inst.strike),
      delta=pick.get("delta"),
      confidence=signal.confidence,
    )
    await self._execute_signal(signal, is_expiry=self._is_expiry_day())
    if self.positions.has_open_for_token(inst.token):
      self._last_flip_at = datetime.now(tz=timezone.utc)
      await self.risk.record_flip()

  async def _execute_signal(self, signal: CandidateSignal, *, is_expiry: bool) -> None:
    token = signal.instrument_token
    if self.positions.has_open_for_token(token) or token in self._entering_tokens:
      logger.info(
        "entry_skipped_duplicate_guard",
        tsym=signal.tsym,
        token=token,
        already_open=self.positions.has_open_for_token(token),
        entering=token in self._entering_tokens,
      )
      return

    self._entering_tokens.add(token)
    try:
      await self._execute_signal_locked(signal, is_expiry=is_expiry)
    finally:
      self._entering_tokens.discard(token)

  async def _execute_signal_locked(
    self, signal: CandidateSignal, *, is_expiry: bool
  ) -> None:
    token = signal.instrument_token
    # Re-check after any await gap from the caller path.
    if self.positions.has_open_for_token(token):
      return

    risk_snap = await self.risk.ensure_daily_state()
    if risk_snap.kill_switch or risk_snap.entries_blocked:
      await self._journal.write_system_event(
        SystemEvent(
          event_type="entry_skipped",
          ts=signal.ts,
          severity="warning",
          message=f"Signal {signal.setup_type} {signal.side} skipped — entries blocked",
          metadata={
            "setup": signal.setup_type,
            "side": signal.side,
            "tsym": signal.tsym,
            "confidence": signal.confidence,
            "kill_switch": risk_snap.kill_switch,
            "entries_blocked": risk_snap.entries_blocked,
            "block_reason": risk_snap.block_reason,
          },
        )
      )
      return

    await self._journal.write_candidate_signal(signal)

    opt_for_signal = self._option_data.get(token)
    if opt_for_signal is None or opt_for_signal.ltp is None:
      # Flip path may have just resolved from band map — synthesize OptionState.
      ltp = signal.scanner_metadata.get("option_ltp")
      if ltp is None:
        return
      opt_for_signal = OptionState(
        instrument_token=token,
        tsym=signal.tsym,
        ltp=Decimal(str(ltp)),
      )

    if self.positions.has_open_for_token(token):
      return

    validation = self.validator.validate(
      signal,
      opt_for_signal,
      has_open_for_token=self.positions.has_open_for_token(token),
      # Opposite-side flip must not be blocked by the exit we just did.
      in_cooldown=(
        False
        if signal.setup_type == "trend_reversal_flip"
        else self.positions.in_cooldown(token)
      ),
      kill_switch=risk_snap.kill_switch,
      is_expiry_day=is_expiry,
    )
    await self._journal.write_validation(validation)
    if not validation.passed:
      reasons = list(validation.rejection_reasons or [])
      self._last_entry_block = ", ".join(reasons)[:240] if reasons else "validation_failed"
      return

    # Phase 5: TTE min-confidence bump for near-expiry
    extra = (signal.feature_snapshot.extra or {}) if signal.feature_snapshot else {}
    bucket = extra.get("expiry_bucket")
    if bucket:
      from algocrypto.option_data.vol_model import ExpiryBucket

      try:
        eb = ExpiryBucket(bucket)
      except ValueError:
        eb = ExpiryBucket.UNKNOWN
      tte_adj = tte_entry_adjustments(eb, self._config.strategy.get("vol_model"))
      min_conf = self.quality.min_confidence + int(tte_adj.get("min_confidence_add", 0))
      if int(signal.confidence or 0) < min_conf:
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=signal.ts,
            severity="debug",
            message="NO_TRADE",
            metadata={
              "selected_strategy": "NO_TRADE",
              "selected_reason": f"tte_min_confidence={signal.confidence} < {min_conf}",
              "trade_allowed": False,
              "expiry_bucket": bucket,
              "tte_adjustments": tte_adj,
            },
          )
        )
        return
      signal.scanner_metadata = {
        **(signal.scanner_metadata or {}),
        "tte_adjustments": tte_adj,
        "expiry_bucket": bucket,
        "strategy_family": strategy_family(signal.setup_type),
      }

    # L2 orderbook + executable price / slippage (Gap-Fix Phase 3)
    sizing_preview_lots = 1
    raw_book: dict = {}
    ask_depth_lots = 0
    try:
      from algocrypto.execution.price_model import (
        estimate_executable,
        liquidity_capped_lots,
        slippage_acceptable,
      )
      from algocrypto.fees import orderbook_entry_ok, parse_l2_orderbook
      from algocrypto.risk.engine import lots_for_confidence

      sizing_preview_lots = lots_for_confidence(
        self._config.risk,
        int(signal.confidence or signal.scanner_metadata.get("confidence", 0)),
      )
      book_fn = getattr(self._broker, "get_l2_orderbook", None)
      if callable(book_fn) and bool((self._config.fees.get("orderbook") or {}).get("enabled", True)):
        depth = int((self._config.fees.get("orderbook") or {}).get("depth", 10))
        raw_book = await book_fn(token, depth) or {}
        book = parse_l2_orderbook(token, raw_book)
        ask_depth_lots = int(book.ask_depth_lots or 0)
        ok_book, book_reasons = orderbook_entry_ok(
          book,
          order_lots=sizing_preview_lots,
          fees_cfg=self._config.fees,
        )
        if book.best_ask is not None and opt_for_signal.ltp is not None:
          if ok_book:
            opt_for_signal = OptionState(
              instrument_token=opt_for_signal.instrument_token,
              tsym=opt_for_signal.tsym,
              ltp=book.best_ask,
              bid=book.best_bid,
              ask=book.best_ask,
            )
        if not ok_book:
          block = ", ".join(book_reasons)
          self._last_entry_block = block[:240]
          await self._journal.write_notification(
            "trade",
            "warning",
            "Entry blocked by orderbook",
            f"{signal.tsym}: {', '.join(book_reasons)}",
          )
          logger.info(
            "entry_blocked_orderbook",
            tsym=signal.tsym,
            reasons=book_reasons,
            ask_size=book.ask_size,
            ask_depth=book.ask_depth_lots,
          )
          return

        exe_quote = estimate_executable(
          side="BUY",
          order_lots=sizing_preview_lots,
          book_payload=raw_book,
          reference_ltp=opt_for_signal.ltp,
          exec_cfg=self._config.execution,
        )
        ok_slip, slip_reason = slippage_acceptable(
          exe_quote, exec_cfg=self._config.execution
        )
        signal.scanner_metadata = {
          **(signal.scanner_metadata or {}),
          "expected_entry_price": (
            str(exe_quote.expected_price) if exe_quote.expected_price else None
          ),
          "expected_slippage": str(exe_quote.expected_slippage),
          "expected_slippage_pct": str(exe_quote.expected_slippage_pct),
          "spread_pct": str(exe_quote.spread_pct) if exe_quote.spread_pct else None,
          "executable_reasons": list(exe_quote.reasons),
        }
        if not ok_slip:
          self._last_entry_block = (slip_reason or "excessive_expected_slippage")[:240]
          await self._journal.write_system_event(
            SystemEvent(
              event_type="strategy_decision",
              ts=signal.ts,
              severity="warning",
              message="NO_TRADE",
              metadata={
                "selected_strategy": "NO_TRADE",
                "selected_reason": slip_reason or "excessive_expected_slippage",
                "trade_allowed": False,
                "tsym": signal.tsym,
                "expected_slippage_pct": str(exe_quote.expected_slippage_pct),
                "spread_pct": str(exe_quote.spread_pct) if exe_quote.spread_pct else None,
              },
            )
          )
          logger.info(
            "entry_blocked_slippage",
            tsym=signal.tsym,
            reason=slip_reason,
            slippage_pct=str(exe_quote.expected_slippage_pct),
          )
          return
        if exe_quote.expected_price is not None:
          opt_for_signal = OptionState(
            instrument_token=opt_for_signal.instrument_token,
            tsym=opt_for_signal.tsym,
            ltp=exe_quote.expected_price,
            bid=exe_quote.best_bid,
            ask=exe_quote.best_ask,
          )
    except Exception:
      logger.exception("orderbook_gate_failed", tsym=signal.tsym)

    # Liquidity preview for risk sizing (cap before size_entry)
    liq_cap: int | None = None
    if ask_depth_lots > 0:
      try:
        from algocrypto.execution.price_model import liquidity_capped_lots

        # Pass a high desired to get pure depth cap
        liq_cap, _ = liquidity_capped_lots(
          desired_lots=10**6,
          ask_or_bid_depth=ask_depth_lots,
          exec_cfg=self._config.execution,
        )
      except Exception:
        liq_cap = None

    sizing = await self.risk.size_entry(
      signal,
      opt_for_signal,
      risk_snap,
      open_position_count=self.positions.open_count,
      liquidity_lots=liq_cap,
      portfolio_positions=self.positions.open_positions,
    )
    if not sizing.approved:
      self._last_entry_block = (sizing.rejection_reason or "risk_rejected")[:240]
      await self._journal.write_notification(
        "trade",
        "warning",
        "Entry blocked by risk",
        sizing.rejection_reason or "risk_rejected",
      )
      if sizing.rejection_reason and sizing.rejection_reason.startswith("portfolio_"):
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=signal.ts,
            severity="warning",
            message="NO_TRADE",
            metadata={
              "selected_strategy": "NO_TRADE",
              "selected_reason": sizing.rejection_reason,
              "trade_allowed": False,
              "size_breakdown": sizing.size_breakdown,
            },
          )
        )
      return

    if sizing.size_breakdown:
      signal.scanner_metadata = {
        **(signal.scanner_metadata or {}),
        "size_breakdown": sizing.size_breakdown,
        "binding_reason": sizing.binding_reason,
      }

    # Phase 5: apply TTE size multiplier after risk sizing
    tte_meta = (signal.scanner_metadata or {}).get("tte_adjustments") or {}
    size_mult = float(tte_meta.get("size_mult", 1.0))
    if 0 < size_mult < 1.0 and sizing.quantity > 1:
      new_qty = max(1, int(sizing.quantity * size_mult))
      if new_qty < sizing.quantity:
        from algocrypto.symbols_util import premium_usd

        sizing.lots = new_qty
        sizing.quantity = new_qty
        sizing.premium_required = premium_usd(
          price=sizing.entry_ltp, lots=new_qty, size=sizing.contract_size
        )
        signal.scanner_metadata = {
          **(signal.scanner_metadata or {}),
          "tte_size_mult": size_mult,
          "tte_sized_lots": new_qty,
        }

    # Phase 6: Expected Value gate (never treats rule_score as P(win))
    ev_cfg = self._config.strategy.get("ev_engine") or {}
    if bool(ev_cfg.get("enabled", True)):
      prem = float(sizing.premium_required or 0)
      rule_score = int(signal.confidence or 0)
      resolved = resolve_ev_engine_config(
        ev_cfg,
        is_paper=self._config.is_paper,
        rule_score=rule_score,
      )
      fee_pct = resolved["assumed_fee_pct_of_premium"]
      slip = float((signal.scanner_metadata or {}).get("expected_slippage") or 0)
      ev = estimate_ev(
        rule_score=rule_score,
        strategy=signal.setup_type,
        entry_premium_usd=prem,
        fees_usd=prem * fee_pct,
        expected_slippage_usd=slip,
        learner_snapshot=self.learner.snapshot(),
        adverse_pct=resolved["adverse_pct"],
        reward_pct=resolved["reward_pct"],
        min_ev=resolved["min_ev"],
        min_trades_for_pwin=resolved["min_trades_for_pwin"],
        prior_pwin=resolved["prior_pwin"],
      )
      ev_eligible = ev.eligible or bool(resolved.get("skip_ev_block"))
      ev_reason = None if ev_eligible else ev.reason
      signal.scanner_metadata = {
        **(signal.scanner_metadata or {}),
        "ev": {
          "expected_value": round(ev.expected_value, 4),
          "pwin": ev.estimated_win_probability,
          "eligible": ev_eligible,
          "reason": ev_reason,
          "ev_mode": resolved.get("ev_mode"),
          "paper_bypass": bool(resolved.get("skip_ev_block")),
          **ev.detail,
        },
      }
      await self._journal.write_system_event(
        SystemEvent(
          event_type="ev_estimate",
          ts=signal.ts,
          severity="info" if ev_eligible else "warning",
          message="eligible" if ev_eligible else (ev.reason or "negative_expected_value"),
          metadata={
            "setup": signal.setup_type,
            "tsym": signal.tsym,
            "expected_value": round(ev.expected_value, 4),
            "pwin": ev.estimated_win_probability,
            "eligible": ev_eligible,
            "ev_mode": resolved.get("ev_mode"),
            "paper_bypass": bool(resolved.get("skip_ev_block")),
            **ev.detail,
          },
        )
      )
      if not ev_eligible:
        self._last_entry_block = (ev.reason or "negative_expected_value")[:240]
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=signal.ts,
            severity="warning",
            message="NO_TRADE",
            metadata={
              "selected_strategy": "NO_TRADE",
              "selected_reason": ev.reason or "negative_expected_value",
              "trade_allowed": False,
              "expected_value": round(ev.expected_value, 4),
            },
          )
        )
        return

    # Phase 6: build entry thesis for monitoring
    feat = signal.feature_snapshot
    extra_f = (feat.extra or {}) if feat else {}
    thesis = build_thesis_from_signal(
      side=signal.side,
      strategy=signal.setup_type,
      features_extra=extra_f,
      regime_primary=(signal.scanner_metadata or {}).get("regime_primary"),
      bias=feat.bias_5m.value if feat and feat.bias_5m else None,
      spot=feat.nifty_spot if feat else None,
      vwap=feat.session_vwap if feat else None,
      iv_regime=extra_f.get("iv_regime"),
    )
    self._pending_thesis = thesis

    # Final guard immediately before capital reserve + fill.
    if self.positions.has_open_for_token(token):
      logger.info(
        "entry_aborted_token_opened_during_sizing",
        tsym=signal.tsym,
        token=token,
      )
      return

    await self.risk.reserve_capital(sizing.premium_required)
    try:
      position_id, order_id, update = await self.execution.enter(signal, sizing)
      fill_qty = int(update.filled_qty or sizing.quantity)
      fill_px = update.fill_price or sizing.entry_ltp
      # Use actual fill size for position / margin (partial fills).
      if fill_qty != sizing.quantity and fill_qty > 0:
        from algocrypto.symbols_util import premium_usd

        sizing.lots = fill_qty
        sizing.quantity = fill_qty
        sizing.premium_required = premium_usd(
          price=fill_px, lots=fill_qty, size=sizing.contract_size
        )
      self.positions.register_open(
        position_id,
        order_id,
        signal.id,
        signal,
        sizing,
        fill_px,
        thesis=self._pending_thesis,
      )
      self._last_entry_block = None
      self._pending_thesis = None
      # reconcile_margin syncs deployed to live open premiums (handles unused reserve)
      await self.risk.reconcile_margin(self.positions.total_deployed())
    except Exception:
      # Unwind reserve without bumping trade_count (reconcile from open premiums).
      self._pending_thesis = None
      await self.risk.reconcile_margin(self.positions.total_deployed())
      logger.exception("paper_entry_failed", tsym=signal.tsym)
      raise

  def _is_expiry_day(self) -> bool:
    if self._universe is None or not self._universe.expiry_symbol:
      return False
    exp = parse_expiry_tag(self._universe.expiry_symbol)
    if exp is None:
      return False
    return exp == datetime.now(IST).date()

  def _market_open(self) -> bool:
    from algocrypto.market_session import is_market_open

    return is_market_open(self._config.market_session)
