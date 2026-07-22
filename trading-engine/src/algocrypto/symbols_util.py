"""Helpers for symbols_config.yaml (BTC/ETH Delta schema)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def primary_underlying(symbols: dict[str, Any]) -> str:
    unders = symbols.get("underlyings") or ["BTC"]
    return str(symbols.get("primary_underlying") or unders[0]).upper()


def index_symbol(symbols: dict[str, Any], underlying: str | None = None) -> str:
    u = (underlying or primary_underlying(symbols)).upper()
    return str((symbols.get("index_symbols") or {}).get(u, f"{u}USD"))


def strike_step(symbols: dict[str, Any], underlying: str | None = None) -> float:
    u = (underlying or primary_underlying(symbols)).upper()
    defaults = symbols.get("strike_step_defaults") or {}
    if u in defaults:
        return float(defaults[u])
    if "strike_step" in symbols:
        return float(symbols["strike_step"])
    return 200.0 if u == "BTC" else 20.0


def strike_band_points(symbols: dict[str, Any], underlying: str | None = None) -> float:
    step = strike_step(symbols, underlying)
    band_steps = int(symbols.get("atm_band_steps", 8))
    if "strike_band_points" in symbols:
        return float(symbols["strike_band_points"])
    return step * band_steps


def contract_size(symbols: dict[str, Any], underlying: str | None = None) -> Decimal:
    """Underlying units per 1 Delta lot (BTC=0.001, ETH=0.01)."""
    u = (underlying or primary_underlying(symbols)).upper()
    defaults = symbols.get("contract_size_defaults") or {}
    if u in defaults:
        return Decimal(str(defaults[u]))
    return Decimal("0.001") if u == "BTC" else Decimal("0.01")


def account_capital_usd(risk: dict[str, Any]) -> Decimal:
    if "account_capital_usd" in risk:
        return Decimal(str(risk["account_capital_usd"]))
    # Legacy key from Algo-Flat fork
    return Decimal(str(risk.get("account_capital_inr", 250)))


def premium_usd(*, price: Decimal, lots: int, size: Decimal) -> Decimal:
    """Long-option funds required ≈ premium × lots × contract_size (Delta)."""
    if lots < 1 or price <= 0 or size <= 0:
        return Decimal("0")
    return price * Decimal(lots) * size


def underlying_from_tsym(tsym: str) -> str:
    """C-BTC-64600-200726 → BTC, P-ETH-… → ETH."""
    parts = str(tsym or "").upper().split("-")
    if len(parts) >= 2 and parts[1] in ("BTC", "ETH"):
        return parts[1]
    return "BTC"


def pnl_usd(*, entry: Decimal, exit: Decimal, lots: int, size: Decimal) -> Decimal:
    return (exit - entry) * Decimal(lots) * size
