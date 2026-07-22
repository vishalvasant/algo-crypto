"""Delta options fee + orderbook helpers."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class FeeQuote:
    notional_usd: Decimal
    premium_usd: Decimal
    raw_fee_usd: Decimal
    capped_fee_usd: Decimal
    gst_usd: Decimal
    total_fee_usd: Decimal
    rate_used: Decimal
    role: str
    capped: bool


@dataclass(frozen=True)
class OrderBookSnapshot:
    symbol: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_size: int
    ask_size: int
    bid_depth_lots: int
    ask_depth_lots: int
    mid: Decimal | None
    spread_pct: Decimal | None
    imbalance: Decimal | None  # (bid-ask)/(bid+ask) size at top
    raw: dict[str, Any]


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def option_fee(
    *,
    spot: Decimal,
    premium: Decimal,
    lots: int,
    contract_size: Decimal,
    fees_cfg: dict[str, Any],
    role: str | None = None,
    product_taker_rate: Decimal | None = None,
    product_maker_rate: Decimal | None = None,
) -> FeeQuote:
    """Fee = min(notional × rate, premium × cap%) × (1 + GST).

    Notional = lots × contract_size × spot (BTC/ETH underlying units × index).
    """
    role_s = (role or str(fees_cfg.get("assume_role", "taker"))).lower()
    if role_s == "maker":
        rate = product_maker_rate if product_maker_rate is not None else _d(
            fees_cfg.get("options_maker_rate", "0.0001")
        )
    else:
        rate = product_taker_rate if product_taker_rate is not None else _d(
            fees_cfg.get("options_taker_rate", "0.0001")
        )
        role_s = "taker"

    notional = Decimal(lots) * contract_size * spot
    premium_usd = premium * Decimal(lots) * contract_size
    raw = notional * rate
    cap_pct = _d(fees_cfg.get("premium_fee_cap_pct", "3.5")) / Decimal("100")
    cap = premium_usd * cap_pct
    capped = raw > cap and premium_usd > 0
    base = cap if capped else raw
    gst_pct = _d(fees_cfg.get("gst_pct", "18")) / Decimal("100")
    gst = base * gst_pct
    total = base + gst
    return FeeQuote(
        notional_usd=notional,
        premium_usd=premium_usd,
        raw_fee_usd=raw,
        capped_fee_usd=base,
        gst_usd=gst,
        total_fee_usd=total,
        rate_used=rate,
        role=role_s,
        capped=capped,
    )


def parse_l2_orderbook(symbol: str, payload: dict[str, Any]) -> OrderBookSnapshot:
    buys = payload.get("buy") or payload.get("bids") or []
    sells = payload.get("sell") or payload.get("asks") or []

    def _lvl_price(row: Any) -> Decimal | None:
        if isinstance(row, dict):
            return _d(row.get("price"), default="") if row.get("price") not in (None, "") else None
        if isinstance(row, (list, tuple)) and row:
            return _d(row[0])
        return None

    def _lvl_size(row: Any) -> int:
        if isinstance(row, dict):
            return int(float(row.get("size") or 0))
        if isinstance(row, (list, tuple)) and len(row) > 1:
            return int(float(row[1] or 0))
        return 0

    best_bid = _lvl_price(buys[0]) if buys else None
    best_ask = _lvl_price(sells[0]) if sells else None
    bid_size = _lvl_size(buys[0]) if buys else 0
    ask_size = _lvl_size(sells[0]) if sells else 0
    bid_depth = sum(_lvl_size(r) for r in buys)
    ask_depth = sum(_lvl_size(r) for r in sells)

    mid = None
    spread_pct = None
    if best_bid is not None and best_ask is not None and best_ask > 0:
        mid = (best_bid + best_ask) / Decimal("2")
        spread_pct = ((best_ask - best_bid) / best_ask) * Decimal("100")

    imbalance = None
    tot = bid_size + ask_size
    if tot > 0:
        imbalance = Decimal(bid_size - ask_size) / Decimal(tot)

    return OrderBookSnapshot(
        symbol=symbol,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=bid_size,
        ask_size=ask_size,
        bid_depth_lots=bid_depth,
        ask_depth_lots=ask_depth,
        mid=mid,
        spread_pct=spread_pct,
        imbalance=imbalance,
        raw=payload,
    )


def orderbook_entry_ok(
    book: OrderBookSnapshot,
    *,
    order_lots: int,
    fees_cfg: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Gates for buying options (we lift the ask)."""
    ob = fees_cfg.get("orderbook") or {}
    if not bool(ob.get("enabled", True)):
        return True, []
    reasons: list[str] = []
    if book.best_ask is None or book.best_ask <= 0:
        reasons.append("orderbook_no_ask")
        return False, reasons
    min_ask = int(ob.get("min_ask_size_lots", 50))
    if book.ask_size < min_ask:
        reasons.append("orderbook_thin_ask")
    coverage = Decimal(str(ob.get("min_ask_coverage_mult", 1.0)))
    need = Decimal(max(order_lots, 1)) * coverage
    if Decimal(book.ask_depth_lots) < need:
        reasons.append("orderbook_insufficient_ask_depth")
    max_ratio = Decimal(str(ob.get("max_ask_bid_size_ratio", 8.0)))
    if book.bid_size > 0 and Decimal(book.ask_size) / Decimal(book.bid_size) > max_ratio:
        # Extremely ask-heavy top — poor for market buys
        reasons.append("orderbook_ask_heavy")
    return len(reasons) == 0, reasons
