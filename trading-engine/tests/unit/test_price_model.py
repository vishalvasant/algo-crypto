"""Gap-Fix Phase 3: executable price, slippage, liquidity cap."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from algocrypto.broker.paper import PaperBrokerAdapter
from algocrypto.execution.price_model import (
  estimate_executable,
  liquidity_capped_lots,
  slippage_acceptable,
  walk_book,
  BookLevel,
)
from algocrypto.models.events import ExecutionRequest, TradingMode
from datetime import datetime, timezone


def _book(asks=None, bids=None):
  return {
    "sell": asks or [{"price": "100", "size": 50}, {"price": "101", "size": 80}],
    "buy": bids or [{"price": "98", "size": 40}, {"price": "97", "size": 60}],
  }


def test_walk_book_vwap_across_levels():
  levels = [BookLevel(Decimal("100"), 10), BookLevel(Decimal("102"), 10)]
  avg, filled, n, full = walk_book(levels, order_lots=15)
  assert full is True
  assert filled == 15
  assert n == 2
  # 10*100 + 5*102 = 1510 / 15 = 100.666...
  assert avg == Decimal("1510") / Decimal("15")


def test_walk_book_partial_when_thin():
  levels = [BookLevel(Decimal("100"), 5)]
  avg, filled, n, full = walk_book(levels, order_lots=20)
  assert full is False
  assert filled == 5
  assert avg == Decimal("100")


def test_estimate_buy_uses_asks():
  q = estimate_executable(
    side="BUY",
    order_lots=10,
    book_payload=_book(),
    reference_ltp=Decimal("99"),
    exec_cfg={"extra_slippage_bps": 0, "size_impact_slippage_mult": 0},
  )
  assert q.best_ask == Decimal("100")
  assert q.expected_price == Decimal("100")
  assert q.fully_fillable is True
  assert q.expected_slippage >= 0


def test_estimate_sell_uses_bids():
  q = estimate_executable(
    side="SELL",
    order_lots=10,
    book_payload=_book(),
    reference_ltp=Decimal("99"),
    exec_cfg={"extra_slippage_bps": 0, "size_impact_slippage_mult": 0},
  )
  assert q.best_bid == Decimal("98")
  assert q.expected_price == Decimal("98")


def test_slippage_rejects_wide_spread():
  book = {
    "sell": [{"price": "120", "size": 100}],
    "buy": [{"price": "100", "size": 100}],
  }
  q = estimate_executable(
    side="BUY",
    order_lots=10,
    book_payload=book,
    reference_ltp=Decimal("110"),
    exec_cfg={"size_impact_slippage_mult": 0, "extra_slippage_bps": 0},
  )
  ok, reason = slippage_acceptable(
    q, exec_cfg={"max_entry_spread_pct": 5, "max_expected_slippage_pct": 10}
  )
  assert ok is False
  assert reason == "excessive_spread"


def test_slippage_rejects_excessive_slip():
  book = {
    "sell": [{"price": "110", "size": 5}, {"price": "130", "size": 100}],
    "buy": [{"price": "100", "size": 50}],
  }
  q = estimate_executable(
    side="BUY",
    order_lots=50,
    book_payload=book,
    reference_ltp=Decimal("105"),
    exec_cfg={"size_impact_slippage_mult": 0, "extra_slippage_bps": 0},
  )
  ok, reason = slippage_acceptable(
    q,
    exec_cfg={
      "max_entry_spread_pct": 50,
      "max_expected_slippage_pct": 1.0,
      "require_full_book_fill": False,
    },
  )
  assert ok is False
  assert reason == "excessive_expected_slippage"


def test_liquidity_capped_lots():
  lots, reason = liquidity_capped_lots(
    desired_lots=100,
    ask_or_bid_depth=50,
    exec_cfg={"max_order_pct_of_depth": 40},
  )
  assert lots == 20  # 40% of 50
  assert reason == "liquidity_size_cap"


@pytest.mark.asyncio
async def test_paper_walk_fill_and_partial():
  data = MagicMock()
  data.is_connected = True
  data.get_l2_orderbook = AsyncMock(
    return_value={
      "sell": [{"price": "500", "size": 10}, {"price": "510", "size": 10}],
      "buy": [{"price": "490", "size": 20}],
    }
  )
  cfg = SimpleNamespace(
    fees={"orderbook": {"depth": 10}},
    paper_trading={
      "fill_model": "orderbook_walk",
      "partial_fills": True,
      "simulate_latency_ms": 0,
      "extra_slippage_bps": 0,
      "min_fill_ratio": 0.3,
      "reject_on_insufficient_depth": True,
      "rejection_probability": 0,
    },
    execution={
      "extra_slippage_bps": 0,
      "size_impact_slippage_mult": 0,
      "ltp_fallback_slippage_bps": 0,
    },
  )
  paper = PaperBrokerAdapter(cfg, data)  # type: ignore[arg-type]
  req = ExecutionRequest(
    client_order_id="AC-TEST12345678",
    ts=datetime.now(tz=timezone.utc),
    instrument_token="P-BTC",
    exchange="DELTA",
    tsym="P-BTC-77000",
    side="BUY",
    quantity=15,
    order_type="MKT",
    product="MIS",
    reference_ltp=Decimal("495"),
    mode=TradingMode.PAPER,
  )
  upd = await paper.place_order(req)
  assert upd.status == "COMPLETE"
  assert upd.filled_qty == 15
  # 10*500 + 5*510 = 7550/15
  assert upd.fill_price == Decimal("7550") / Decimal("15")

  # Thin book → partial
  data.get_l2_orderbook = AsyncMock(
    return_value={"sell": [{"price": "500", "size": 8}], "buy": [{"price": "490", "size": 20}]}
  )
  req2 = req.model_copy(update={"quantity": 20, "client_order_id": "AC-TEST87654321"})
  upd2 = await paper.place_order(req2)
  assert upd2.status == "PARTIAL"
  assert upd2.filled_qty == 8


@pytest.mark.asyncio
async def test_paper_rejects_too_thin():
  data = MagicMock()
  data.is_connected = True
  data.get_l2_orderbook = AsyncMock(
    return_value={"sell": [{"price": "500", "size": 2}], "buy": [{"price": "490", "size": 20}]}
  )
  cfg = SimpleNamespace(
    fees={"orderbook": {"depth": 10}},
    paper_trading={
      "fill_model": "orderbook_walk",
      "partial_fills": True,
      "simulate_latency_ms": 0,
      "extra_slippage_bps": 0,
      "min_fill_ratio": 0.5,
      "rejection_probability": 0,
    },
    execution={"size_impact_slippage_mult": 0, "extra_slippage_bps": 0},
  )
  paper = PaperBrokerAdapter(cfg, data)  # type: ignore[arg-type]
  req = ExecutionRequest(
    client_order_id="AC-THIN00000001",
    ts=datetime.now(tz=timezone.utc),
    instrument_token="P-BTC",
    exchange="DELTA",
    tsym="P-BTC-77000",
    side="BUY",
    quantity=20,
    order_type="MKT",
    product="MIS",
    reference_ltp=Decimal("495"),
    mode=TradingMode.PAPER,
  )
  upd = await paper.place_order(req)
  assert upd.status == "REJECTED"
  assert upd.rejection_reason == "insufficient_depth_min_fill_ratio"
