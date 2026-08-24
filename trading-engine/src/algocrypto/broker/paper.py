from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from algocrypto.broker.base import BrokerAdapter
from algocrypto.config import AppConfig
from algocrypto.execution.price_model import estimate_executable, extract_levels, walk_book
from algocrypto.models.events import (
  Candle,
  CandleInterval,
  ExecutionRequest,
  OrderUpdate,
  QuoteUpdate,
  TradingMode,
)

logger = structlog.get_logger(__name__)


class PaperBrokerAdapter(BrokerAdapter):
  """Uses a real data adapter for quotes; simulates fills with book realism."""

  def __init__(self, config: AppConfig, data_adapter: BrokerAdapter) -> None:
    self._config = config
    self._data = data_adapter
    self._ltp_cache: dict[str, Decimal] = {}

  @property
  def is_connected(self) -> bool:
    return self._data.is_connected

  @property
  def websocket_open(self) -> bool:
    return bool(getattr(self._data, "websocket_open", False))

  async def connect(self) -> None:
    await self._data.connect()

  async def disconnect(self) -> None:
    await self._data.disconnect()

  async def stop_websocket(self) -> None:
    stop = getattr(self._data, "stop_websocket", None)
    if callable(stop):
      await stop()
    else:
      await self._data.disconnect()

  async def get_candles(
    self,
    exchange: str,
    token: str,
    interval: CandleInterval,
    start: datetime,
    end: datetime,
  ) -> list[Candle]:
    return await self._data.get_candles(exchange, token, interval, start, end)

  async def get_quotes(self, exchange: str, token: str) -> dict[str, Any]:
    return await self._data.get_quotes(exchange, token)

  async def search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:
    search = getattr(self._data, "search_scrip", None)
    if search is None:
      return []
    return await search(exchange, search_text)

  async def get_option_chain(
    self,
    exchange: str,
    tradingsymbol: str,
    strikeprice: float,
    count: int,
  ) -> list[dict[str, Any]]:
    return await self._data.get_option_chain(exchange, tradingsymbol, strikeprice, count)

  async def get_option_tickers(self, **kwargs: Any) -> list[dict[str, Any]]:
    fn = getattr(self._data, "get_option_tickers", None)
    if fn is None:
      return []
    return await fn(**kwargs)

  async def get_index_ticker(self, symbol: str) -> dict[str, Any]:
    fn = getattr(self._data, "get_index_ticker", None)
    if fn is None:
      return {}
    return await fn(symbol)

  async def get_products(self, **kwargs: Any) -> list[dict[str, Any]]:
    fn = getattr(self._data, "get_products", None)
    if fn is None:
      return []
    return await fn(**kwargs)

  async def get_l2_orderbook(self, symbol: str, depth: int = 10) -> dict[str, Any]:
    fn = getattr(self._data, "get_l2_orderbook", None)
    if fn is None:
      return {}
    return await fn(symbol, depth)

  async def subscribe(
    self,
    instruments: list[str],
    on_quote: Any,
    on_order: Any | None = None,
  ) -> None:
    def _wrapped(quote: QuoteUpdate) -> None:
      if quote.ltp is not None:
        self._ltp_cache[quote.instrument_token] = quote.ltp
      on_quote(quote)

    await self._data.subscribe(instruments, _wrapped, on_order)

  def _paper_cfg(self) -> dict:
    return self._config.paper_trading or {}

  def _exec_cfg(self) -> dict:
    return self._config.execution or {}

  async def place_order(self, request: ExecutionRequest) -> OrderUpdate:
    paper = self._paper_cfg()
    exec_cfg = dict(self._exec_cfg())
    if "extra_slippage_bps" in paper:
      exec_cfg["extra_slippage_bps"] = paper["extra_slippage_bps"]

    latency_ms = int(paper.get("simulate_latency_ms", 0) or 0)
    if latency_ms > 0:
      await asyncio.sleep(latency_ms / 1000.0)

    reject_p = float(paper.get("rejection_probability", 0) or 0)
    if reject_p > 0 and random.random() < reject_p:
      logger.warning("paper_order_rejected_random", tsym=request.tsym)
      return self._reject(request, "simulated_rejection", latency_ms)

    fill_model = str(paper.get("fill_model", "orderbook_walk")).lower()
    partial_ok = bool(paper.get("partial_fills", True))
    min_fill_ratio = Decimal(str(paper.get("min_fill_ratio", 0.5)))
    reject_thin = bool(paper.get("reject_on_insufficient_depth", True))

    raw_book: dict[str, Any] = {}
    book_fn = getattr(self._data, "get_l2_orderbook", None)
    if callable(book_fn):
      try:
        depth = int((self._config.fees.get("orderbook") or {}).get("depth", 10))
        raw_book = await book_fn(request.instrument_token, depth) or {}
      except Exception:
        logger.exception("paper_orderbook_fetch_failed", tsym=request.tsym)

    ref = request.reference_ltp
    if ref is None or ref <= 0:
      ref = self._ltp_cache.get(request.instrument_token)

    quote = estimate_executable(
      side=request.side,
      order_lots=max(int(request.quantity), 1),
      book_payload=raw_book,
      reference_ltp=ref,
      exec_cfg=exec_cfg,
    )

    filled_qty = int(request.quantity)
    fill_price = quote.expected_price

    if fill_model == "ltp":
      fill_price = ref or quote.expected_price
      filled_qty = int(request.quantity)
    elif fill_model == "top_of_book":
      if request.side.upper() == "BUY":
        fill_price = quote.best_ask or ref
      else:
        fill_price = quote.best_bid or ref
      filled_qty = int(request.quantity)
    else:
      bids, asks = extract_levels(raw_book)
      levels = asks if request.side.upper() == "BUY" else bids
      avg, filled, _, full = walk_book(levels, order_lots=max(int(request.quantity), 1))
      if avg is not None:
        fill_price = avg
        extra_bps = Decimal(str(paper.get("extra_slippage_bps", 0) or 0))
        if extra_bps > 0:
          bump = fill_price * extra_bps / Decimal("10000")
          fill_price = (
            fill_price + bump
            if request.side.upper() == "BUY"
            else max(Decimal("0.01"), fill_price - bump)
          )
        if full:
          filled_qty = int(request.quantity)
        elif partial_ok:
          filled_qty = filled
          ratio = Decimal(filled) / Decimal(max(int(request.quantity), 1))
          if ratio < min_fill_ratio:
            return self._reject(
              request, "insufficient_depth_min_fill_ratio", latency_ms
            )
        else:
          if reject_thin:
            return self._reject(request, "insufficient_depth", latency_ms)
          filled_qty = filled
      elif quote.expected_price is not None:
        fill_price = quote.expected_price
        filled_qty = int(request.quantity)
      else:
        return self._reject(request, "no_executable_price", latency_ms)

    if fill_price is None or fill_price <= 0:
      return self._reject(request, "invalid_fill_price", latency_ms)
    if filled_qty < 1:
      return self._reject(request, "zero_fill", latency_ms)

    now = datetime.now(tz=timezone.utc)
    ref_px = ref or fill_price
    raw_slip = fill_price - ref_px
    if request.side.upper() == "BUY":
      slip = max(Decimal("0"), raw_slip)
    else:
      slip = max(Decimal("0"), -raw_slip)

    status = "COMPLETE" if filled_qty >= int(request.quantity) else "PARTIAL"
    logger.info(
      "paper_order_filled",
      client_order_id=request.client_order_id,
      side=request.side,
      fill_price=str(fill_price),
      filled_qty=filled_qty,
      requested_qty=request.quantity,
      status=status,
      slippage=str(slip),
      fill_model=fill_model,
      spread_pct=str(quote.spread_pct) if quote.spread_pct is not None else None,
      reasons=list(quote.reasons),
    )
    return OrderUpdate(
      ts=now,
      client_order_id=request.client_order_id,
      broker_order_id=f"PAPER-{request.client_order_id[-8:]}",
      status=status,
      report_type="Fill",
      fill_price=fill_price,
      filled_qty=filled_qty,
      avg_price=fill_price,
      slippage=slip,
      latency_ms=latency_ms,
      mode=TradingMode.PAPER,
    )

  def _reject(
    self, request: ExecutionRequest, reason: str, latency_ms: int
  ) -> OrderUpdate:
    now = datetime.now(tz=timezone.utc)
    logger.warning(
      "paper_order_rejected",
      client_order_id=request.client_order_id,
      reason=reason,
      tsym=request.tsym,
    )
    return OrderUpdate(
      ts=now,
      client_order_id=request.client_order_id,
      broker_order_id=f"PAPER-RJ-{request.client_order_id[-6:]}",
      status="REJECTED",
      report_type="Rejected",
      fill_price=None,
      filled_qty=0,
      avg_price=None,
      slippage=None,
      latency_ms=latency_ms,
      mode=TradingMode.PAPER,
      rejection_reason=reason,
    )
