from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog

from algocrypto.config import AppConfig
from algocrypto.db.connection import get_pool
from algocrypto.db.paper_account import ensure_paper_account
from algocrypto.models.events import CandidateSignal, OptionState
from algocrypto.risk.circuit_breakers import (
  CircuitDecision,
  CircuitSnapshot,
  evaluate_circuit_breakers,
  post_trade_halt_reason,
)
from algocrypto.risk.resolve import resolve_risk_config
from algocrypto.risk.states import RiskState
from algocrypto.symbols_util import account_capital_usd, premium_usd

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class EntrySizing:
  approved: bool
  quantity: int  # number of Delta lots
  lot_size: int  # always 1 for Delta (qty is already in lots)
  entry_ltp: Decimal
  premium_required: Decimal
  rejection_reason: str | None = None
  lots: int = 0
  confidence: int = 0
  contract_size: Decimal = Decimal("0.001")
  size_breakdown: dict | None = None
  binding_reason: str | None = None


def lots_for_confidence(risk_cfg: dict, confidence: int) -> int:
  """Map signal confidence (0–100) → number of lots from config tiers."""
  default = max(1, int(risk_cfg.get("default_lots", 1)))
  sizing = risk_cfg.get("confidence_lot_sizing") or {}
  if not sizing.get("enabled", False):
    return default

  tiers = list(sizing.get("tiers") or [])
  tiers_sorted = sorted(
    tiers,
    key=lambda t: int(t.get("min_confidence", 0)),
    reverse=True,
  )
  chosen = default
  for tier in tiers_sorted:
    if confidence >= int(tier.get("min_confidence", 0)):
      chosen = max(1, int(tier.get("lots", default)))
      break

  max_lots = int(sizing.get("max_lots", chosen))
  return max(1, min(chosen, max_lots))


def fit_lots_to_capital(
  risk_cfg: dict,
  *,
  confidence: int,
  entry_ltp: Decimal,
  contract_size: Decimal,
  available: Decimal,
  deployed: Decimal,
  equity: Decimal,
) -> tuple[int, Decimal]:
  """Confidence lots, then fill remaining deploy room when confidence is high."""
  target = lots_for_confidence(risk_cfg, confidence)
  max_lots = int((risk_cfg.get("confidence_lot_sizing") or {}).get("max_lots", target))
  max_pct = Decimal(
    str(
      risk_cfg.get(
        "max_premium_deployed_pct",
        risk_cfg.get("max_premium_pct_of_available", 85),
      )
    )
  )
  max_for_trade = available * max_pct / Decimal("100")
  deploy_pct = Decimal(
    str(
      risk_cfg.get(
        "max_exposure_pct",
        risk_cfg.get("max_deployed_pct_of_equity", 85),
      )
    )
  )
  deploy_cap = equity * deploy_pct / Decimal("100")
  room = deploy_cap - deployed

  def _ok(n: int) -> Decimal | None:
    if n < 1 or contract_size <= 0 or entry_ltp <= 0:
      return None
    prem = premium_usd(price=entry_ltp, lots=n, size=contract_size)
    if prem <= available and prem <= max_for_trade and prem <= room:
      return prem
    return None

  lots = target
  while lots >= 1 and _ok(lots) is None:
    lots -= 1
  if lots < 1:
    return 0, Decimal("0")

  if confidence >= 80:
    while lots < max_lots and _ok(lots + 1) is not None:
      lots += 1

  prem = _ok(lots)
  assert prem is not None
  return lots, prem


def _parse_risk_state(raw: str | None) -> RiskState:
  try:
    return RiskState(str(raw or "NORMAL"))
  except ValueError:
    return RiskState.NORMAL


def _ts_within_hour(timestamps: list, *, now: datetime | None = None) -> list[datetime]:
  now = now or datetime.now(tz=timezone.utc)
  cutoff = now - timedelta(hours=1)
  out: list[datetime] = []
  for ts in timestamps or []:
    if ts is None:
      continue
    if getattr(ts, "tzinfo", None) is None:
      ts = ts.replace(tzinfo=timezone.utc)
    if ts >= cutoff:
      out.append(ts)
  return out


@dataclass
class DailyRiskSnapshot:
  trade_date: date
  starting_capital: Decimal
  available_capital: Decimal
  deployed_capital: Decimal
  realized_pnl: Decimal
  trade_count: int
  consecutive_losses: int
  kill_switch: bool
  entries_blocked: bool
  block_reason: str | None = None
  flip_count: int = 0
  flips_disabled: bool = False
  risk_state: RiskState = RiskState.NORMAL
  losing_trade_timestamps: list = field(default_factory=list)
  flip_timestamps: list = field(default_factory=list)

  @property
  def equity(self) -> Decimal:
    return self.starting_capital + self.realized_pnl

  @property
  def auto_trade_enabled(self) -> bool:
    return not self.kill_switch and not self.entries_blocked

  @property
  def losing_trades_last_hour(self) -> int:
    return len(_ts_within_hour(self.losing_trade_timestamps))

  @property
  def flips_last_hour(self) -> int:
    return len(_ts_within_hour(self.flip_timestamps))


class RiskEngine:
  def __init__(self, config: AppConfig) -> None:
    self._config = config
    self._risk = config.risk

  def _risk_cfg(self) -> dict:
    return resolve_risk_config(self._risk, is_paper=self._config.is_paper)

  def _today_ist(self) -> date:
    return datetime.now(IST).date()

  def evaluate_gates(
    self,
    snapshot: DailyRiskSnapshot,
    *,
    open_position_count: int = 0,
    is_flip: bool = False,
  ) -> CircuitDecision:
    risk_cfg = self._risk_cfg()
    snap = snapshot
    # Paper learning mode: ignore sticky circuit-breaker halts (keep kill_switch).
    if risk_cfg.get("paper_disable_circuit_breakers") and not snapshot.kill_switch:
      snap = DailyRiskSnapshot(
        trade_date=snapshot.trade_date,
        starting_capital=snapshot.starting_capital,
        available_capital=snapshot.available_capital,
        deployed_capital=snapshot.deployed_capital,
        realized_pnl=snapshot.realized_pnl,
        trade_count=snapshot.trade_count,
        consecutive_losses=snapshot.consecutive_losses,
        kill_switch=False,
        entries_blocked=False,
        block_reason=None,
        flip_count=snapshot.flip_count,
        flips_disabled=False,
        risk_state=RiskState.NORMAL,
        losing_trade_timestamps=snapshot.losing_trade_timestamps,
        flip_timestamps=snapshot.flip_timestamps,
      )
    return evaluate_circuit_breakers(
      risk_cfg,
      CircuitSnapshot(
        starting_capital=snap.starting_capital,
        realized_pnl=snap.realized_pnl,
        trade_count=snap.trade_count,
        consecutive_losses=snap.consecutive_losses,
        open_position_count=open_position_count,
        flip_count=snap.flip_count,
        flips_last_hour=snap.flips_last_hour,
        losing_trades_last_hour=snap.losing_trades_last_hour,
        kill_switch=snap.kill_switch,
        entries_blocked=snap.entries_blocked,
        risk_state=snap.risk_state,
        flips_disabled=snap.flips_disabled,
      ),
      is_flip=is_flip,
    )

  async def clear_paper_circuit_halt(self) -> None:
    """Unblock entries after circuit halt when paper circuit breakers are disabled."""
    if not self._risk_cfg().get("paper_disable_circuit_breakers"):
      return
    today = self._today_ist()
    pool = get_pool()
    async with pool.acquire() as conn:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
          entries_blocked = FALSE,
          flips_disabled = FALSE,
          risk_state = 'NORMAL',
          block_reason = NULL,
          updated_at = now()
        WHERE trade_date = $1 AND NOT kill_switch
        """,
        today,
      )

  async def ensure_daily_state(self) -> DailyRiskSnapshot:
    capital = account_capital_usd(self._risk)
    await ensure_paper_account(capital)
    today = self._today_ist()
    pool = get_pool()
    async with pool.acquire() as conn:
      row = await conn.fetchrow(
        """
        SELECT trade_date, starting_capital, available_capital, deployed_capital,
               realized_pnl, trade_count, consecutive_losses, kill_switch,
               entries_blocked, block_reason,
               COALESCE(flip_count, 0) AS flip_count,
               COALESCE(flips_disabled, FALSE) AS flips_disabled,
               COALESCE(risk_state, 'NORMAL') AS risk_state,
               COALESCE(losing_trade_timestamps, '{}') AS losing_trade_timestamps,
               COALESCE(flip_timestamps, '{}') AS flip_timestamps
        FROM daily_risk_state WHERE trade_date = $1
        """,
        today,
      )
    assert row is not None
    return DailyRiskSnapshot(
      trade_date=row["trade_date"],
      starting_capital=Decimal(str(row["starting_capital"] or capital)),
      available_capital=Decimal(str(row["available_capital"] or capital)),
      deployed_capital=Decimal(str(row["deployed_capital"] or 0)),
      realized_pnl=Decimal(str(row["realized_pnl"] or 0)),
      trade_count=int(row["trade_count"] or 0),
      consecutive_losses=int(row["consecutive_losses"] or 0),
      kill_switch=bool(row["kill_switch"]),
      entries_blocked=bool(row["entries_blocked"]),
      block_reason=row["block_reason"],
      flip_count=int(row["flip_count"] or 0),
      flips_disabled=bool(row["flips_disabled"]),
      risk_state=_parse_risk_state(row["risk_state"]),
      losing_trade_timestamps=list(row["losing_trade_timestamps"] or []),
      flip_timestamps=list(row["flip_timestamps"] or []),
    )

  async def set_auto_trade(self, enabled: bool) -> DailyRiskSnapshot:
    """Enable/disable new entries without kill-switch flatten."""
    today = self._today_ist()
    pool = get_pool()
    await self.ensure_daily_state()
    async with pool.acquire() as conn:
      row = await conn.fetchrow(
        "SELECT kill_switch FROM daily_risk_state WHERE trade_date = $1",
        today,
      )
      kill = bool(row["kill_switch"]) if row else False
      if enabled:
        if kill:
          await conn.execute(
            """
            UPDATE daily_risk_state SET
              block_reason = COALESCE(block_reason, 'kill_switch'),
              updated_at = now()
            WHERE trade_date = $1
            """,
            today,
          )
        else:
          await conn.execute(
            """
            UPDATE daily_risk_state SET
              entries_blocked = FALSE,
              flips_disabled = FALSE,
              risk_state = 'NORMAL',
              block_reason = NULL,
              updated_at = now()
            WHERE trade_date = $1
            """,
            today,
          )
          await conn.execute(
            """
            INSERT INTO notifications (type, severity, title, message)
            VALUES ('system', 'info', 'Auto trading ON',
                    'Engine may place entries when setups fire')
            """
          )
      else:
        await conn.execute(
          """
          UPDATE daily_risk_state SET
            entries_blocked = TRUE,
            risk_state = 'HALTED',
            block_reason = 'auto_trade_off',
            updated_at = now()
          WHERE trade_date = $1
          """,
          today,
        )
        await conn.execute(
          """
          INSERT INTO notifications (type, severity, title, message)
          VALUES ('system', 'warning', 'Auto trading OFF',
                  'New entries paused — scans and decision logs continue')
          """
        )
    return await self.ensure_daily_state()

  async def halt_entries(
    self,
    reason: str,
    *,
    state: RiskState = RiskState.HALTED,
    flips_only: bool = False,
  ) -> DailyRiskSnapshot:
    """Hard-block entries (or flips only) and journal the transition."""
    today = self._today_ist()
    pool = get_pool()
    import json

    meta = json.dumps(
      {"reason": reason, "state": state.value, "flips_only": flips_only}
    )
    async with pool.acquire() as conn:
      if flips_only:
        await conn.execute(
          """
          UPDATE daily_risk_state SET
            flips_disabled = TRUE,
            updated_at = now()
          WHERE trade_date = $1
          """,
          today,
        )
      else:
        await conn.execute(
          """
          UPDATE daily_risk_state SET
            entries_blocked = TRUE,
            risk_state = $3,
            block_reason = $2,
            updated_at = now()
          WHERE trade_date = $1
          """,
          today,
          reason,
          state.value,
        )
      await conn.execute(
        """
        INSERT INTO system_events (event_type, severity, message, metadata)
        VALUES ('risk_state_change', 'warning', $1, $2::jsonb)
        """,
        f"Risk → {state.value}: {reason}",
        meta,
      )
      await conn.execute(
        """
        INSERT INTO notifications (type, severity, title, message)
        VALUES ('risk', 'warning', $1, $2)
        """,
        f"Risk {state.value}",
        reason,
      )
    logger.warning("risk_halt", reason=reason, state=state.value, flips_only=flips_only)
    return await self.ensure_daily_state()

  async def record_flip(self) -> DailyRiskSnapshot:
    """Increment flip counters; disable further flips if caps hit."""
    today = self._today_ist()
    now = datetime.now(tz=timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
          flip_count = COALESCE(flip_count, 0) + 1,
          flip_timestamps = COALESCE(flip_timestamps, '{}') || ARRAY[$2::timestamptz],
          updated_at = now()
        WHERE trade_date = $1
        """,
        today,
        now,
      )
    snap = await self.ensure_daily_state()
    max_day = int(self._risk.get("max_flips_per_day") or 0)
    max_hour = int(self._risk.get("max_flips_per_hour") or 0)
    if (max_day > 0 and snap.flip_count >= max_day) or (
      max_hour > 0 and snap.flips_last_hour >= max_hour
    ):
      reason = (
        "max_flips_per_day"
        if max_day > 0 and snap.flip_count >= max_day
        else "max_flips_per_hour"
      )
      await self.halt_entries(reason, flips_only=True)
      return await self.ensure_daily_state()
    return snap

  async def size_entry(
    self,
    signal: CandidateSignal,
    option: OptionState,
    snapshot: DailyRiskSnapshot,
    *,
    open_position_count: int = 0,
    is_flip: bool = False,
    liquidity_lots: int | None = None,
    portfolio_positions: list | None = None,
  ) -> EntrySizing:
    from algocrypto.risk.portfolio import (
      build_portfolio_snapshot,
      evaluate_portfolio_entry,
    )
    from algocrypto.risk.sizing import compute_lot_size
    from algocrypto.symbols_util import underlying_from_tsym

    lot_size = 1
    try:
      contract_size = Decimal(str(signal.scanner_metadata.get("contract_size", "0.001")))
    except Exception:
      contract_size = Decimal("0.001")
    if contract_size <= 0:
      contract_size = Decimal("0.001")
    confidence = int(
      signal.confidence
      if signal.confidence is not None
      else signal.scanner_metadata.get("confidence", 0)
    )
    entry_ltp = option.ltp or Decimal("0")

    def _reject(
      reason: str,
      lots: int = 0,
      premium: Decimal | None = None,
      breakdown: dict | None = None,
    ) -> EntrySizing:
      qty = max(lots, 0)
      prem = premium if premium is not None else Decimal("0")
      return EntrySizing(
        False, qty, lot_size, entry_ltp, prem, reason,
        lots=lots, confidence=confidence, contract_size=contract_size,
        size_breakdown=breakdown, binding_reason=reason,
      )

    if entry_ltp <= 0:
      return _reject("invalid_ltp")

    is_flip = is_flip or signal.setup_type == "trend_reversal_flip"
    gate = self.evaluate_gates(
      snapshot, open_position_count=open_position_count, is_flip=is_flip
    )
    if not gate.allow_entry:
      return _reject(gate.reason or "circuit_breaker")

    exit_cfg = getattr(self._config, "position_exit", None) or {}
    breakdown = compute_lot_size(
      self._risk_cfg(),
      confidence=confidence,
      entry_ltp=entry_ltp,
      contract_size=contract_size,
      available=snapshot.available_capital,
      deployed=snapshot.deployed_capital,
      equity=snapshot.equity,
      exit_cfg=exit_cfg,
      liquidity_lots=liquidity_lots,
    )
    bd = {
      "confidence_lots": breakdown.confidence_lots,
      "risk_lots": breakdown.risk_lots,
      "capital_lots": breakdown.capital_lots,
      "liquidity_lots": breakdown.liquidity_lots,
      "max_lots": breakdown.max_lots,
      "final_lots": breakdown.final_lots,
      "binding_reason": breakdown.binding_reason,
      "limits": breakdown.limits,
      "notes": breakdown.notes,
    }

    if breakdown.final_lots < 1:
      return _reject(
        breakdown.binding_reason or "insufficient_capital",
        lots=breakdown.confidence_lots,
        premium=premium_usd(
          price=entry_ltp, lots=max(breakdown.confidence_lots, 1), size=contract_size
        ),
        breakdown=bd,
      )

    lots = breakdown.final_lots
    premium = premium_usd(price=entry_ltp, lots=lots, size=contract_size)

    # Portfolio exposure gate (after size known)
    und = str(
      signal.scanner_metadata.get("underlying")
      or underlying_from_tsym(signal.tsym)
      or "BTC"
    )
    port_snap = build_portfolio_snapshot(portfolio_positions or [])
    delta = None
    pick = (signal.scanner_metadata or {}).get("strike_pick") or {}
    if isinstance(pick, dict) and pick.get("delta") is not None:
      try:
        delta = float(pick["delta"]) * lots
      except (TypeError, ValueError):
        delta = None
    port_dec = evaluate_portfolio_entry(
      self._risk,
      snapshot=port_snap,
      equity=snapshot.equity,
      new_underlying=und,
      new_side=signal.side,
      new_premium=premium,
      new_delta=delta,
    )
    if not port_dec.allow:
      bd["portfolio"] = port_dec.details
      return _reject(
        port_dec.reason or "portfolio_exposure_limit",
        lots=lots,
        premium=premium,
        breakdown=bd,
      )

    logger.info(
      "entry_sized",
      confidence=confidence,
      lots=lots,
      quantity=lots,
      contract_size=str(contract_size),
      premium_usd=str(premium),
      binding_reason=breakdown.binding_reason,
      size_breakdown=bd,
      risk_state=gate.state.value,
      warnings=list(gate.warning_reasons),
    )
    return EntrySizing(
      True, lots, lot_size, entry_ltp, premium, None,
      lots=lots, confidence=confidence, contract_size=contract_size,
      size_breakdown=bd, binding_reason=breakdown.binding_reason,
    )

  async def reserve_capital(self, premium: Decimal) -> None:
    today = self._today_ist()
    pool = get_pool()
    async with pool.acquire() as conn:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
            deployed_capital = deployed_capital + $2,
            available_capital = available_capital - $2,
            updated_at = now()
        WHERE trade_date = $1
        """,
        today,
        premium,
      )

  async def release_capital(self, premium: Decimal, pnl: Decimal) -> None:
    today = self._today_ist()
    now = datetime.now(tz=timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
      if pnl < 0:
        await conn.execute(
          """
          UPDATE daily_risk_state SET
              deployed_capital = GREATEST(deployed_capital - $2, 0),
              available_capital = available_capital + $2 + $3,
              realized_pnl = realized_pnl + $3,
              trade_count = trade_count + 1,
              consecutive_losses = consecutive_losses + 1,
              losing_trade_timestamps =
                COALESCE(losing_trade_timestamps, '{}') || ARRAY[$4::timestamptz],
              updated_at = now()
          WHERE trade_date = $1
          """,
          today,
          premium,
          pnl,
          now,
        )
      else:
        await conn.execute(
          """
          UPDATE daily_risk_state SET
              deployed_capital = GREATEST(deployed_capital - $2, 0),
              available_capital = available_capital + $2 + $3,
              realized_pnl = realized_pnl + $3,
              trade_count = trade_count + 1,
              consecutive_losses = 0,
              updated_at = now()
          WHERE trade_date = $1
          """,
          today,
          premium,
          pnl,
        )

    snap = await self.ensure_daily_state()
    risk_cfg = self._risk_cfg()
    halt_reason = None
    if not risk_cfg.get("paper_disable_circuit_breakers"):
      halt_reason = post_trade_halt_reason(
        risk_cfg,
        starting_capital=snap.starting_capital,
        realized_pnl_after=snap.realized_pnl,
        trade_count_after=snap.trade_count,
        consecutive_losses_after=snap.consecutive_losses,
        losing_trades_last_hour=snap.losing_trades_last_hour,
      )
    if halt_reason:
      await self.halt_entries(halt_reason, state=RiskState.HALTED)
      if halt_reason == "daily_loss_limit" and bool(
        risk_cfg.get("emergency_flatten_on_daily_loss", False)
      ):
        await self.halt_entries(
          "daily_loss_limit_emergency",
          state=RiskState.EMERGENCY_FLATTEN,
        )

  async def reconcile_margin(self, open_deployed: Decimal) -> DailyRiskSnapshot:
    today = self._today_ist()
    deployed = max(Decimal("0"), open_deployed)
    pool = get_pool()
    async with pool.acquire() as conn:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
            deployed_capital = $2,
            available_capital = GREATEST(
              COALESCE(starting_capital, 0) + COALESCE(realized_pnl, 0) - $2,
              0
            ),
            updated_at = now()
        WHERE trade_date = $1
        """,
        today,
        deployed,
      )
    return await self.ensure_daily_state()

  def is_force_exit_time(self) -> bool:
    force = self._risk.get("force_exit_time", None)
    if force in (None, "", False, "null", "none", "None"):
      return False
    try:
      hh, mm = map(int, str(force).split(":"))
    except Exception:
      return False
    now = datetime.now(IST).time()
    return now >= time(hh, mm)
