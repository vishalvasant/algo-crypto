"""Executable price + expected slippage (Gap-Fix Phase 3 / §16–17).

Conservative pricing: BUY ≈ lift asks, SELL ≈ hit bids. Never treat LTP alone
as executable when a book is available.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class BookLevel:
  price: Decimal
  size: int


@dataclass(frozen=True)
class ExecutableQuote:
  side: str  # BUY | SELL
  reference_ltp: Decimal | None
  best_bid: Decimal | None
  best_ask: Decimal | None
  mid: Decimal | None
  spread_pct: Decimal | None
  expected_price: Decimal | None
  expected_slippage: Decimal  # vs mid (or LTP if mid missing); signed for BUY adverse +
  expected_slippage_pct: Decimal
  depth_lots_used: int
  levels_consumed: int
  fillable_lots: int
  fully_fillable: bool
  reasons: tuple[str, ...]


def _d(value: Any, default: str = "0") -> Decimal:
  try:
    if value in (None, ""):
      return Decimal(default)
    return Decimal(str(value))
  except Exception:
    return Decimal(default)


def extract_levels(payload: dict[str, Any]) -> tuple[list[BookLevel], list[BookLevel]]:
  """Return (bids desc, asks asc) from Delta-style L2 payload."""
  buys = payload.get("buy") or payload.get("bids") or []
  sells = payload.get("sell") or payload.get("asks") or []

  def _parse(row: Any) -> BookLevel | None:
    if isinstance(row, dict):
      p = row.get("price")
      s = row.get("size") or 0
      if p in (None, ""):
        return None
      return BookLevel(_d(p), int(float(s)))
    if isinstance(row, (list, tuple)) and row:
      return BookLevel(_d(row[0]), int(float(row[1] if len(row) > 1 else 0)))
    return None

  bids: list[BookLevel] = []
  for row in buys:
    lvl = _parse(row)
    if lvl and lvl.price > 0 and lvl.size > 0:
      bids.append(lvl)
  asks: list[BookLevel] = []
  for row in sells:
    lvl = _parse(row)
    if lvl and lvl.price > 0 and lvl.size > 0:
      asks.append(lvl)
  return bids, asks


def walk_book(
  levels: list[BookLevel],
  *,
  order_lots: int,
) -> tuple[Decimal | None, int, int, bool]:
  """VWAP across levels. Returns (avg_price, lots_filled, levels_used, fully_filled)."""
  if order_lots < 1 or not levels:
    return None, 0, 0, False
  remaining = order_lots
  notional = Decimal("0")
  filled = 0
  used = 0
  for lvl in levels:
    take = min(remaining, lvl.size)
    if take <= 0:
      continue
    notional += lvl.price * Decimal(take)
    filled += take
    remaining -= take
    used += 1
    if remaining <= 0:
      break
  if filled < 1:
    return None, 0, 0, False
  avg = notional / Decimal(filled)
  return avg, filled, used, remaining <= 0


def estimate_executable(
  *,
  side: str,
  order_lots: int,
  book_payload: dict[str, Any] | None,
  reference_ltp: Decimal | None,
  exec_cfg: dict[str, Any] | None = None,
) -> ExecutableQuote:
  """Conservative executable quote for decision + paper realism."""
  cfg = exec_cfg or {}
  side_u = side.upper()
  bids, asks = extract_levels(book_payload or {})
  best_bid = bids[0].price if bids else None
  best_ask = asks[0].price if asks else None
  mid = None
  spread_pct = None
  if best_bid is not None and best_ask is not None and best_ask > 0:
    mid = (best_bid + best_ask) / Decimal("2")
    spread_pct = ((best_ask - best_bid) / best_ask) * Decimal("100")

  reasons: list[str] = []
  levels = asks if side_u == "BUY" else bids
  avg, filled, n_levels, full = walk_book(levels, order_lots=max(order_lots, 1))

  expected = avg
  if expected is None:
    if side_u == "BUY" and best_ask is not None:
      expected = best_ask
      reasons.append("fallback_best_ask")
    elif side_u == "SELL" and best_bid is not None:
      expected = best_bid
      reasons.append("fallback_best_bid")
    elif reference_ltp is not None:
      # Last resort — mark as LTP (not preferred).
      slip_bps = Decimal(str(cfg.get("ltp_fallback_slippage_bps", 15)))
      bump = reference_ltp * slip_bps / Decimal("10000")
      expected = reference_ltp + bump if side_u == "BUY" else reference_ltp - bump
      reasons.append("ltp_fallback_with_slippage_bps")
      filled = max(order_lots, 1)
      full = True

  if not full and order_lots > 0:
    reasons.append("insufficient_depth_partial")

  # Slippage vs mid (adverse positive for costs).
  slip = Decimal("0")
  slip_pct = Decimal("0")
  ref = mid if mid is not None else reference_ltp
  if expected is not None and ref is not None and ref > 0:
    raw = expected - ref
    # Adverse: BUY pays above mid, SELL receives below mid
    slip = raw if side_u == "BUY" else -raw
    if slip < 0:
      slip = Decimal("0")  # favorable — treat as 0 expected adverse
    slip_pct = (slip / ref) * Decimal("100")

  # Extra model add-ons from config (bps of expected price).
  extra_bps = Decimal(str(cfg.get("extra_slippage_bps", 0)))
  if expected is not None and extra_bps > 0:
    extra = expected * extra_bps / Decimal("10000")
    slip += extra
    if expected > 0:
      slip_pct = (slip / expected) * Decimal("100")
    if side_u == "BUY":
      expected = expected + extra
    else:
      expected = expected - extra
    reasons.append("extra_slippage_bps")

  # Size vs depth penalty
  depth = sum(l.size for l in levels)
  size_mult = Decimal(str(cfg.get("size_impact_slippage_mult", 0.5)))
  if depth > 0 and order_lots > 0 and expected is not None:
    impact = (Decimal(order_lots) / Decimal(depth)) * size_mult
    # impact as fraction of spread or 1% of price
    base = (spread_pct / Decimal("100") * expected) if spread_pct else (expected * Decimal("0.01"))
    add = base * impact
    if add > 0:
      slip += add
      if side_u == "BUY":
        expected = expected + add
      else:
        expected = max(Decimal("0.01"), expected - add)
      reasons.append("size_impact")

  return ExecutableQuote(
    side=side_u,
    reference_ltp=reference_ltp,
    best_bid=best_bid,
    best_ask=best_ask,
    mid=mid,
    spread_pct=spread_pct,
    expected_price=expected,
    expected_slippage=slip,
    expected_slippage_pct=slip_pct,
    depth_lots_used=filled,
    levels_consumed=n_levels,
    fillable_lots=filled,
    fully_fillable=full,
    reasons=tuple(reasons),
  )


def slippage_acceptable(
  quote: ExecutableQuote,
  *,
  exec_cfg: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
  cfg = exec_cfg or {}
  max_pct = Decimal(str(cfg.get("max_expected_slippage_pct", 3.0)))
  max_spread = Decimal(str(cfg.get("max_entry_spread_pct", 12.0)))
  require_full = bool(cfg.get("require_full_book_fill", True))

  if quote.expected_price is None or quote.expected_price <= 0:
    return False, "no_executable_price"
  if quote.spread_pct is not None and quote.spread_pct > max_spread:
    return False, "excessive_spread"
  if quote.expected_slippage_pct > max_pct:
    return False, "excessive_expected_slippage"
  if require_full and not quote.fully_fillable:
    return False, "insufficient_book_depth"
  return True, None


def liquidity_capped_lots(
  *,
  desired_lots: int,
  ask_or_bid_depth: int,
  exec_cfg: dict[str, Any] | None = None,
) -> tuple[int, str | None]:
  """Cap size to a fraction of visible depth."""
  cfg = exec_cfg or {}
  if desired_lots < 1:
    return 0, "desired_lots_zero"
  max_frac = Decimal(str(cfg.get("max_order_pct_of_depth", 40))) / Decimal("100")
  if ask_or_bid_depth < 1:
    # No book — leave desired (other gates may reject)
    return desired_lots, None
  cap = max(1, int(Decimal(ask_or_bid_depth) * max_frac))
  if desired_lots > cap:
    return cap, "liquidity_size_cap"
  return desired_lots, None
