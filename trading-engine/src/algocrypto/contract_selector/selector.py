"""Delta BTC/ETH option universe — daily expiry ATM band."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from algocrypto.broker.base import BrokerAdapter
from algocrypto.config import AppConfig
from algocrypto.models.events import Instrument

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class ContractUniverse:
    spot: Decimal
    atm_strike: Decimal
    expiry_symbol: str | None = None  # DD-MM-YYYY
    underlying: str = "BTC"
    instruments: list[Instrument] = field(default_factory=list)
    atm_ce: Instrument | None = None
    atm_pe: Instrument | None = None
    subscription_keys: list[str] = field(default_factory=list)
    spots_by_underlying: dict[str, Decimal] = field(default_factory=dict)


def _parse_option_symbol(symbol: str) -> tuple[str, str, Decimal, date] | None:
    """C-BTC-90000-310125 → (CE, BTC, 90000, date)."""
    parts = symbol.split("-")
    if len(parts) < 4:
        return None
    side_raw, under, strike_s, exp_s = parts[0], parts[1], parts[2], parts[3]
    side = "CE" if side_raw.upper().startswith("C") else "PE" if side_raw.upper().startswith("P") else None
    if side is None:
        return None
    try:
        strike = Decimal(strike_s)
        exp = datetime.strptime(exp_s, "%d%m%y").date()
    except Exception:
        return None
    return side, under.upper(), strike, exp


def _expiry_ddmmyyyy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


class ContractSelector:
    def __init__(self, config: AppConfig, broker: BrokerAdapter) -> None:
        self._config = config
        self._broker = broker

    def atm_strike_for_spot(self, spot: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            step = Decimal("100")
        return (spot / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step

    def retarget_atm(self, universe: ContractUniverse, spot: Decimal) -> ContractUniverse:
        under = universe.underlying
        steps = Decimal(
            str(
                (self._config.symbols.get("strike_step_defaults") or {}).get(under, 100)
            )
        )
        atm = self.atm_strike_for_spot(spot, steps)
        atm_ce = next(
            (i for i in universe.instruments if i.strike == atm and i.option_type == "CE"),
            None,
        )
        atm_pe = next(
            (i for i in universe.instruments if i.strike == atm and i.option_type == "PE"),
            None,
        )
        if atm_ce is None:
            ces = [i for i in universe.instruments if i.option_type == "CE"]
            atm_ce = min(ces, key=lambda i: abs(i.strike - atm), default=None)
        if atm_pe is None:
            pes = [i for i in universe.instruments if i.option_type == "PE"]
            atm_pe = min(pes, key=lambda i: abs(i.strike - atm), default=None)
        return ContractUniverse(
            spot=spot,
            atm_strike=atm,
            expiry_symbol=universe.expiry_symbol,
            underlying=under,
            instruments=universe.instruments,
            atm_ce=atm_ce,
            atm_pe=atm_pe,
            subscription_keys=universe.subscription_keys,
            spots_by_underlying=dict(universe.spots_by_underlying),
        )

    async def _spot_for(self, underlying: str) -> Decimal | None:
        idx_map = self._config.symbols.get("index_symbols") or {}
        symbol = str(idx_map.get(underlying) or f"{underlying}USD")
        # Prefer perpetual mark as proxy if index fails
        broker = self._broker
        get_index = getattr(broker, "get_index_ticker", None)
        if callable(get_index):
            try:
                row = await get_index(symbol)
                px = row.get("mark_price") or row.get("close") or row.get("price")
                if px is not None:
                    return Decimal(str(px))
            except Exception:
                logger.warning("index_fetch_failed", symbol=symbol)
        # Fallback: BTCUSD / ETHUSD perpetual ticker
        perp = f"{underlying}USD"
        try:
            q = await broker.get_quotes("DELTA", perp)
            if q.get("lp") is not None:
                return Decimal(str(q["lp"]))
        except Exception:
            pass
        return None

    async def _nearest_expiry(self, underlying: str) -> date | None:
        get_tickers = getattr(self._broker, "get_option_tickers", None)
        if not callable(get_tickers):
            return None
        today = datetime.now(IST).date()
        lookahead = int(self._config.symbols.get("max_expiry_lookahead_days", 3))
        for offset in range(0, lookahead + 1):
            d = today + timedelta(days=offset)
            # After 17:30 IST on expiry day, skip today
            now = datetime.now(IST)
            if offset == 0 and (now.hour > 17 or (now.hour == 17 and now.minute >= 30)):
                continue
            expiry = _expiry_ddmmyyyy(d)
            try:
                rows = await get_tickers(underlying=underlying, expiry_date=expiry)
            except Exception:
                continue
            if rows:
                return d
        return None

    async def build_universe(self, spot: Decimal | None = None) -> ContractUniverse:
        underlyings = list(self._config.symbols.get("underlyings") or ["BTC", "ETH"])
        primary = str(self._config.symbols.get("primary_underlying") or underlyings[0]).upper()
        spots: dict[str, Decimal] = {}
        for u in underlyings:
            px = await self._spot_for(str(u).upper())
            if px is not None:
                spots[str(u).upper()] = px
        if primary not in spots and spots:
            primary = next(iter(spots))
        if primary not in spots:
            # last resort so engine can boot offline
            fallback = spot or Decimal("100000")
            spots[primary] = fallback
            logger.warning("spot_fallback", underlying=primary, spot=str(fallback))

        primary_spot = spots[primary]
        expiry_d = await self._nearest_expiry(primary)
        if expiry_d is None:
            expiry_d = datetime.now(IST).date() + timedelta(days=1)
            logger.warning("expiry_fallback", expiry=str(expiry_d))

        expiry_tag = _expiry_ddmmyyyy(expiry_d)
        step_defaults = self._config.symbols.get("strike_step_defaults") or {}
        band_steps = int(self._config.symbols.get("atm_band_steps", 8))
        get_tickers = getattr(self._broker, "get_option_tickers", None)

        instruments: list[Instrument] = []
        for under, under_spot in spots.items():
            step = Decimal(str(step_defaults.get(under, 100)))
            atm = self.atm_strike_for_spot(under_spot, step)
            lo = atm - step * band_steps
            hi = atm + step * band_steps
            rows: list[dict[str, Any]] = []
            if callable(get_tickers):
                try:
                    rows = await get_tickers(underlying=under, expiry_date=expiry_tag)
                except Exception:
                    logger.exception("option_chain_fetch_failed", underlying=under)
            for row in rows:
                symbol = str(row.get("symbol") or "")
                parsed = _parse_option_symbol(symbol)
                if parsed is None:
                    continue
                side, u2, strike, exp = parsed
                if u2 != under or exp != expiry_d:
                    continue
                if strike < lo or strike > hi:
                    continue
                product_id = str(row.get("product_id") or row.get("id") or symbol)
                from algocrypto.symbols_util import contract_size as default_contract_size

                size = default_contract_size(self._config.symbols, under)
                raw_cv = row.get("contract_value")
                if raw_cv not in (None, ""):
                    try:
                        parsed_size = Decimal(str(raw_cv))
                        if parsed_size > 0:
                            size = parsed_size
                    except Exception:
                        pass
                instruments.append(
                    Instrument(
                        exchange="DELTA",
                        token=symbol,  # Delta symbol e.g. C-BTC-64400-200726
                        tsym=symbol,
                        underlying=under,
                        expiry_date=datetime(exp.year, exp.month, exp.day, tzinfo=timezone.utc),
                        strike=strike,
                        option_type=side,
                        lot_size=1,
                        contract_size=size,
                        is_atm=strike == atm,
                        in_band=True,
                    )
                )

        # Primary ATM CE/PE
        step = Decimal(str(step_defaults.get(primary, 100)))
        atm = self.atm_strike_for_spot(primary_spot, step)
        atm_ce = next(
            (
                i
                for i in instruments
                if i.underlying == primary and i.option_type == "CE" and i.strike == atm
            ),
            None,
        )
        atm_pe = next(
            (
                i
                for i in instruments
                if i.underlying == primary and i.option_type == "PE" and i.strike == atm
            ),
            None,
        )
        if atm_ce is None:
            ces = [i for i in instruments if i.underlying == primary and i.option_type == "CE"]
            atm_ce = min(ces, key=lambda i: abs(i.strike - atm), default=None)
        if atm_pe is None:
            pes = [i for i in instruments if i.underlying == primary and i.option_type == "PE"]
            atm_pe = min(pes, key=lambda i: abs(i.strike - atm), default=None)

        index_sym = str((self._config.symbols.get("index_symbols") or {}).get(primary, f"{primary}USD"))
        keys = [BrokerAdapter.format_instrument("DELTA", index_sym)]
        # Also subscribe perpetual for spot continuity
        keys.append(BrokerAdapter.format_instrument("DELTA", f"{primary}USD"))
        keys.extend(BrokerAdapter.format_instrument("DELTA", i.token) for i in instruments)

        universe = ContractUniverse(
            spot=primary_spot,
            atm_strike=atm,
            expiry_symbol=expiry_tag,
            underlying=primary,
            instruments=instruments,
            atm_ce=atm_ce,
            atm_pe=atm_pe,
            subscription_keys=list(dict.fromkeys(keys)),
            spots_by_underlying=spots,
        )
        logger.info(
            "delta_universe_built",
            underlying=primary,
            expiry=expiry_tag,
            spot=str(primary_spot),
            atm=str(atm),
            instruments=len(instruments),
            underlyings=list(spots.keys()),
        )
        return universe

    async def persist_instruments(self, pool, universe: ContractUniverse) -> None:
        if not universe.instruments:
            return
        async with pool.acquire() as conn:
            for inst in universe.instruments:
                await conn.execute(
                    """
                    INSERT INTO instruments (
                        exchange, token, tsym, underlying, expiry_date,
                        strike, option_type, lot_size, is_atm, in_band
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (exchange, token) DO UPDATE SET
                        tsym = EXCLUDED.tsym,
                        strike = EXCLUDED.strike,
                        is_atm = EXCLUDED.is_atm,
                        in_band = EXCLUDED.in_band
                    """,
                    inst.exchange,
                    inst.token,
                    inst.tsym,
                    inst.underlying,
                    inst.expiry_date,
                    inst.strike,
                    inst.option_type,
                    inst.lot_size,
                    inst.is_atm,
                    inst.in_band,
                )
