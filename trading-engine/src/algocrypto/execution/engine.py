from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import structlog

from algocrypto.broker.base import BrokerAdapter
from algocrypto.config import AppConfig
from algocrypto.journal.writer import JournalWriter
from algocrypto.models.events import (
  CandidateSignal,
  ExecutionRequest,
  OrderUpdate,
  TradingMode,
)
from algocrypto.risk.engine import EntrySizing

logger = structlog.get_logger(__name__)


class ExecutionEngine:
  def __init__(self, config: AppConfig, broker: BrokerAdapter, journal: JournalWriter) -> None:
    self._config = config
    self._broker = broker
    self._journal = journal
    self._exec_cfg = config.execution

  async def enter(
    self,
    signal: CandidateSignal,
    sizing: EntrySizing,
  ) -> tuple[UUID, UUID, OrderUpdate]:
    exchange = signal.scanner_metadata.get("exchange", self._config.symbols["exchange_options"])
    client_id = f"AC-{signal.id.hex[:12]}"
    request = ExecutionRequest(
      client_order_id=client_id,
      ts=signal.ts,
      candidate_signal_id=signal.id,
      instrument_token=signal.instrument_token,
      exchange=exchange,
      tsym=signal.tsym,
      side="BUY",
      quantity=sizing.quantity,
      order_type=self._exec_cfg.get("order_type", "MKT"),
      limit_price=sizing.entry_ltp,
      product=self._exec_cfg.get("product", "MIS"),
      reference_ltp=sizing.entry_ltp,
      mode=TradingMode.PAPER if self._config.is_paper else TradingMode.LIVE,
    )

    order_id = await self._journal.write_order_created(request, signal.id)
    update = await self._broker.place_order(request)
    await self._journal.write_order_filled(order_id, update)

    status = (update.status or "").upper()
    if status in ("REJECTED", "CANCELLED", "CANCELED") or (
      update.fill_price is None and int(update.filled_qty or 0) < 1
    ):
      reason = update.rejection_reason or status or "order_rejected"
      await self._journal.write_notification(
        "trade",
        "warning",
        "Entry rejected",
        f"{signal.tsym}: {reason}",
        related_entity="order",
        related_id=str(order_id),
      )
      raise RuntimeError(f"entry_rejected:{reason}")

    fill_qty = int(update.filled_qty or sizing.quantity)
    if fill_qty < 1:
      raise RuntimeError("entry_rejected:zero_fill")

    position_id = await self._journal.write_position_opened(
      order_id=order_id,
      signal=signal,
      fill_price=update.fill_price or sizing.entry_ltp,
      quantity=fill_qty,
      stop_loss=None,
      target=None,
      mode=request.mode.value,
    )

    fill = update.fill_price or sizing.entry_ltp
    # Scale premium to actual fill qty when partial
    if fill_qty != sizing.quantity and sizing.quantity > 0:
      premium = sizing.premium_required * Decimal(fill_qty) / Decimal(sizing.quantity)
    else:
      premium = sizing.premium_required
    size = getattr(sizing, "contract_size", Decimal("0.001"))
    spot = None
    try:
      from algocrypto.fees import option_fee

      meta = signal.scanner_metadata or {}
      spot_raw = meta.get("spot") or meta.get("spot_ltp")
      if spot_raw is not None:
        spot = Decimal(str(spot_raw))
      fee_note = ""
      if spot and spot > 0:
        fq = option_fee(
          spot=spot,
          premium=fill,
          lots=fill_qty,
          contract_size=size,
          fees_cfg=self._config.fees,
        )
        fee_note = f" · est. entry fee ${fq.total_fee_usd:.4f} (taker+GST)"
    except Exception:
      fee_note = ""
    await self._journal.write_notification(
      "trade",
      "info",
      "Trade entry",
      (
        f"Took {signal.tsym} @ ${fill} · "
        f"funds ${premium:.2f} ({fill_qty} lots × {size} "
        f"{signal.scanner_metadata.get('underlying', 'BTC')})"
        f"{fee_note}"
      ),
      related_entity="position",
      related_id=str(position_id),
    )
    logger.info(
      "paper_entry_filled",
      tsym=signal.tsym,
      qty=fill_qty,
      requested=sizing.quantity,
      fill=str(update.fill_price),
      status=status,
      slippage=str(update.slippage) if update.slippage is not None else None,
      position_id=str(position_id),
    )
    return position_id, order_id, update
