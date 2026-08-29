from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog
import uvicorn

from algocrypto.api.health import app as health_app
from algocrypto.api.health import get_engine_state, set_engine_app, set_engine_state
from algocrypto.broker.auth import has_api_credentials, resolve_session
from algocrypto.broker.delta import DeltaAdapter
from algocrypto.broker.paper import PaperBrokerAdapter
from algocrypto.bus.event_bus import EventBus
from algocrypto.config import get_config
from algocrypto.contract_selector.selector import ContractSelector, ContractUniverse
from algocrypto.db.connection import close_pool, get_pool, init_pool
from algocrypto.db.migrate import apply_migrations
from algocrypto.db.paper_account import ensure_paper_account
from algocrypto.journal.writer import JournalWriter
from algocrypto.logging_setup import setup_logging
from algocrypto.market_data.engine import MarketDataEngine
from algocrypto.market_data.poller import RestQuotePoller, quote_from_rest
from algocrypto.models.events import CandleInterval, QuoteUpdate, SystemEvent
from algocrypto.option_data.layer import OptionDataLayer
from algocrypto.symbols_util import account_capital_usd
from algocrypto.trading.orchestrator import TradingOrchestrator

logger = structlog.get_logger(__name__)


class TradingEngineApp:
    def __init__(self) -> None:
        self.config = get_config()
        self.bus = EventBus(max_size=self.config.runtime.get("event_queue_max_size", 10_000))
        self.journal = JournalWriter()
        self._tasks: list[asyncio.Task] = []
        self._universe: ContractUniverse | None = None

        delta = DeltaAdapter(self.config)
        self.broker = (
            PaperBrokerAdapter(self.config, delta)
            if self.config.is_paper
            else delta
        )
        self.market_data = MarketDataEngine(self.config, self.broker, self.bus)
        self.option_data = OptionDataLayer(self.config)
        self.contract_selector = ContractSelector(self.config, self.broker)
        self.orchestrator = TradingOrchestrator(
            self.config,
            self.broker,
            self.journal,
            self.market_data,
            self.option_data,
        )
        self.orchestrator.positions.set_trade_open_hook(self._on_position_opened)
        primary = str(self.config.symbols.get("primary_underlying") or "BTC")
        index_sym = str(
            (self.config.symbols.get("index_symbols") or {}).get(primary, f"{primary}USD")
        )
        self._quote_poller = RestQuotePoller(
            self.broker,
            self.market_data,
            self.option_data,
            self.config.symbols.get("exchange_spot", "DELTA"),
            index_sym,
        )
        self._feed_mode = "offline"
        self._ws_started = False
        self._last_ws_quote_ts: datetime | None = None
        self._ws_retry_after: datetime | None = None
        self._last_universe_refresh_date: date | None = None
        self._last_ws_keys: list[str] = []

    def _on_position_opened(self, _pos: Any) -> None:
        """Ensure the new holding is on the WebSocket feed for tick trails."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._ensure_holdings_on_websocket())
        except RuntimeError:
            pass

    def _has_api_credentials(self) -> bool:
        return has_api_credentials(self.config.env)

    def _has_valid_session(self) -> bool:
        return resolve_session(self.config.env) is not None

    async def _handle_quote(self, quote: QuoteUpdate) -> None:
        await self.market_data.on_quote(quote)
        self.option_data.update_from_quote(quote)
        await self.orchestrator.on_quote(quote)
        if quote.source == "websocket":
            self._feed_mode = "websocket"
            self._last_ws_quote_ts = quote.ts
        set_engine_state(
            {
                "spot_ltp": str(self.market_data.spot_ltp) if self.market_data.spot_ltp else None,
                "last_quote_ts": quote.ts.isoformat(),
                "feed_mode": self._feed_mode,
                "ws_open": bool(getattr(self.broker, "websocket_open", False)),
            }
        )

    def _quote_callback(self, quote: QuoteUpdate) -> None:
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(self._handle_quote(quote), loop)

    async def _stop_subscription(self) -> None:
        # Stop WS only — full disconnect closes the REST client and breaks
        # candle refresh (stale_candle_feed → NO_TRADE).
        stop_ws = getattr(self.broker, "stop_websocket", None)
        if callable(stop_ws):
            await stop_ws()
        else:
            await self.broker.disconnect()
            await self.broker.connect()
        self._ws_started = False
        self._last_ws_keys = []
        if self._feed_mode == "websocket":
            self._feed_mode = "rest"

    def _ws_subscription_keys(self) -> list[str]:
        """Universe ATM band + every open holding (trail exits need tick LTP).

        After ATM retarget / expiry roll, a held strike can fall outside the
        band and drop off WS — then MFE only updates on the ~2s REST poll and
        peaks are missed. Always keep holdings subscribed.
        """
        from algocrypto.broker.base import BrokerAdapter

        keys: list[str] = []
        seen: set[str] = set()

        def _add(key: str) -> None:
            if key and key not in seen:
                seen.add(key)
                keys.append(key)

        if self._universe:
            for k in self._universe.subscription_keys:
                _add(k)

        exchange = str(self.config.symbols.get("exchange_options", "NFO"))
        for pos in self.orchestrator.positions.open_positions:
            _add(BrokerAdapter.format_instrument(exchange, pos.instrument_token))
        return keys

    async def _start_subscription(self) -> None:
        if not self._universe:
            return
        now = datetime.now(tz=timezone.utc)
        if self._ws_retry_after and now < self._ws_retry_after:
            return
        keys = self._ws_subscription_keys()
        if not keys:
            return
        try:
            await self.broker.subscribe(keys, self._quote_callback)
            self._ws_started = True
            self._last_ws_keys = list(keys)
            if getattr(self.broker, "websocket_open", False):
                self._ws_retry_after = None
            else:
                self._ws_retry_after = now + timedelta(seconds=60)
            logger.info(
                "websocket_subscription_ready",
                keys=len(keys),
                open_holdings=self.orchestrator.positions.open_count,
            )
        except Exception:
            logger.exception("websocket_subscribe_failed")
            self._ws_started = False
            self._ws_retry_after = now + timedelta(seconds=60)

    async def _ensure_holdings_on_websocket(self) -> None:
        """Resubscribe if open holdings are missing from the current WS set."""
        if not self._ws_started or not self._is_market_open():
            return
        if self.orchestrator.positions.open_count < 1:
            return
        needed = self._ws_subscription_keys()
        if not needed:
            return
        if needed == self._last_ws_keys:
            return
        try:
            await self.broker.subscribe(needed, self._quote_callback)
            added = [k for k in needed if k not in self._last_ws_keys]
            self._last_ws_keys = list(needed)
            logger.info(
                "websocket_holdings_resubscribed",
                keys=len(needed),
                added=added,
                open_holdings=self.orchestrator.positions.open_count,
            )
        except Exception:
            logger.exception("websocket_holdings_resubscribe_failed")

    def _is_market_open(self) -> bool:
        from algocrypto.market_session import is_market_open

        return is_market_open(self.config.market_session)

    def _ws_stale(self, max_age_sec: int = 8) -> bool:
        last = self._last_ws_quote_ts
        if last is None:
            return True
        return (datetime.now(tz=timezone.utc) - last).total_seconds() > max_age_sec

    async def _poll_rest_quotes_once(self) -> None:
        updated = await self._quote_poller.poll_universe(self._universe)
        if updated:
            if self._ws_stale():
                self._feed_mode = "rest"
            set_engine_state(
                {
                    "spot_ltp": str(self.market_data.spot_ltp)
                    if self.market_data.spot_ltp
                    else None,
                    "last_quote_ts": datetime.now(tz=timezone.utc).isoformat(),
                    "feed_mode": self._feed_mode,
                    "ws_open": bool(getattr(self.broker, "websocket_open", False)),
                }
            )
        # REST path used to update option_data only — drive holding exits too.
        await self._evaluate_open_exits_from_option_data(source="rest")

    async def _poll_open_position_quotes(self) -> None:
        """While WS is healthy, still refresh LTP for open holdings so trails keep up."""
        open_positions = list(self.orchestrator.positions.open_positions)
        if not open_positions:
            return
        exchange = self.config.symbols.get("exchange_options", "NFO")
        for pos in open_positions:
            try:
                raw = await self.broker.get_quotes(exchange, pos.instrument_token)
            except Exception:
                logger.exception("open_position_quote_failed", token=pos.instrument_token)
                continue
            quote = quote_from_rest(exchange, pos.instrument_token, raw)
            if quote is None or quote.ltp is None:
                continue
            quote.tsym = pos.tsym
            self.option_data.update_from_quote(quote)
            await self.orchestrator.on_quote(quote)

    async def _evaluate_open_exits_from_option_data(self, *, source: str) -> None:
        """Push latest option LTPs into PositionManager for trail / reverse exits."""
        for pos in list(self.orchestrator.positions.open_positions):
            state = self.option_data.get(pos.instrument_token)
            if state is None or state.ltp is None:
                continue
            quote = QuoteUpdate(
                ts=datetime.now(tz=timezone.utc),
                exchange=self.config.symbols.get("exchange_options", "NFO"),
                instrument_token=pos.instrument_token,
                tsym=pos.tsym,
                ltp=state.ltp,
                bid=state.bid,
                ask=state.ask,
                source=source,
            )
            await self.orchestrator.on_quote(quote)

    async def _run_rest_poll_loop(self) -> None:
        while True:
            try:
                # Keep open holdings on WS so trail MFE sees ticks, not only 2s REST.
                await self._ensure_holdings_on_websocket()
                # Full chain when WS is down; always keep open holdings ticking for trails.
                if not self._is_market_open() or self._ws_stale():
                    await self._poll_rest_quotes_once()
                elif self.orchestrator.positions.open_count > 0:
                    await self._poll_open_position_quotes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rest_poll_failed")
            interval = int(self.config.runtime.get("rest_quote_poll_interval_seconds", 30))
            if self._is_market_open():
                # Holdings trail wants near-realtime LTP even when WS is up.
                interval = int(
                    self.config.runtime.get("rest_quote_poll_interval_market_seconds", 2)
                )
            await asyncio.sleep(interval)

    async def _resolve_spot(self) -> Decimal:
        spot = await self._refresh_spot_from_rest()
        if spot is not None:
            return spot
        if self.market_data.spot_ltp is not None:
            return self.market_data.spot_ltp
        raw = get_engine_state().get("spot_ltp")
        if raw not in (None, "", "None"):
            try:
                value = Decimal(str(raw))
                if value > 0:
                    return value
            except Exception:
                pass
        # Offline bootstrap only — real spot comes from Delta index/perp.
        return Decimal("100000")

    async def _refresh_universe(self, *, reason: str) -> bool:
        """Rebuild weekly option chain (ATM band) for current / next expiry."""
        from zoneinfo import ZoneInfo

        spot = await self._resolve_spot()

        previous = self._universe.expiry_symbol if self._universe else None
        had_instruments = bool(self._universe and self._universe.instruments)

        universe = await self.contract_selector.build_universe(spot)
        self._universe = universe
        self.orchestrator.set_universe(universe)
        self._last_universe_refresh_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()

        if universe.instruments:
            pool = get_pool()
            await self.contract_selector.persist_instruments(pool, universe)
            # Resubscribe WS if symbols changed or we previously had none.
            if (
                previous != universe.expiry_symbol
                or not had_instruments
                or self._ws_started
            ):
                if self._ws_started:
                    await self._stop_subscription()
                if self._is_market_open():
                    await self._start_subscription()
            await self._poll_rest_quotes_once()

        set_engine_state(
            {
                "instrument_count": len(universe.instruments),
                "atm_strike": str(universe.atm_strike),
                "expiry_symbol": universe.expiry_symbol,
                "spot_ltp": str(self.market_data.spot_ltp or spot),
            }
        )
        logger.info(
            "universe_refreshed",
            reason=reason,
            expiry=universe.expiry_symbol,
            instruments=len(universe.instruments),
            previous_expiry=previous,
            spot=str(spot),
        )
        if universe.instruments and previous != universe.expiry_symbol:
            await self.journal.write_notification(
                "system",
                "info",
                "Daily expiry rolled",
                f"Now trading {universe.expiry_symbol} · {len(universe.instruments)} contracts",
            )
        return bool(universe.instruments)

    async def _maybe_roll_universe(self) -> None:
        """Auto-roll to next daily expiry after cutoff / empty chain / new IST day."""
        from zoneinfo import ZoneInfo

        from algocrypto.contract_selector.expiry import parse_expiry_tag

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        need = False
        reason = "periodic"

        if self._universe is None or not self._universe.instruments:
            need = True
            reason = "empty_universe"
        elif self._last_universe_refresh_date != today:
            need = True
            reason = "new_trading_day"
        else:
            exp = parse_expiry_tag(self._universe.expiry_symbol or "")
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
            if exp is not None and exp < today:
                need = True
                reason = "expiry_passed"
            elif exp == today and (
                now_ist.hour > 17 or (now_ist.hour == 17 and now_ist.minute >= 30)
            ):
                need = True
                reason = "expiry_day_closed"

        if need:
            await self._refresh_universe(reason=reason)

    async def _run_market_feed_loop(self) -> None:
        """Keep Delta WebSocket running for live option ticks (24×7)."""
        while True:
            try:
                await self._maybe_roll_universe()
                if self._is_market_open() and self._universe:
                    if not self._universe.instruments:
                        await self._refresh_universe(reason="market_open_retry")
                    if not self._ws_started or not getattr(self.broker, "websocket_open", False):
                        if not self._ws_started:
                            logger.info("market_open_starting_websocket")
                            await self._start_subscription()
                        elif self._ws_stale(30):
                            logger.info("websocket_stale_restarting")
                            await self._stop_subscription()
                            await self.broker.connect()
                            await self._start_subscription()
                elif not self._is_market_open() and self._ws_started:
                    logger.info("market_closed_stopping_websocket")
                    await self._stop_subscription()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("market_feed_loop_failed")
            await asyncio.sleep(15)

    async def _refresh_spot_from_rest(self) -> Decimal | None:
        from algocrypto.symbols_util import index_symbol

        token = index_symbol(self.config.symbols)
        exchange = self.config.symbols.get("exchange_spot", "DELTA")
        raw = await self.broker.get_quotes(exchange, token)
        quote = quote_from_rest(exchange, token, raw)
        if quote:
            await self.market_data.on_quote(quote)
            if quote.ltp is not None:
                return quote.ltp
        # Perpetual fallback
        under = (self.config.symbols.get("primary_underlying") or "BTC").upper()
        perp = f"{under}USD"
        raw2 = await self.broker.get_quotes(exchange, perp)
        quote2 = quote_from_rest(exchange, perp, raw2)
        if quote2:
            await self.market_data.on_quote(quote2)
            if quote2.ltp is not None:
                return quote2.ltp
        return None

    async def _setup_market_data(self) -> None:
        for field, available in self.option_data.probe_greek_availability().items():
            await self.journal.log_field_availability(field, "websocket", available)

        await self.market_data.backfill_today()
        for interval_candles in self.market_data._candles.values():
            await self.journal.write_candles(interval_candles)

        spot = await self._resolve_spot()
        m1 = self.market_data.candles(CandleInterval.M1)
        if m1 and spot == Decimal("100000"):
            spot = m1[-1].close

        self._universe = await self.contract_selector.build_universe(spot)
        self.orchestrator.set_universe(self._universe)
        from zoneinfo import ZoneInfo

        self._last_universe_refresh_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        pool = get_pool()
        await self.contract_selector.persist_instruments(pool, self._universe)
        await self._poll_rest_quotes_once()

        set_engine_state(
            {
                "status": "running",
                "instrument_count": len(self._universe.instruments),
                "spot_ltp": str(self.market_data.spot_ltp or spot),
                "atm_strike": str(self._universe.atm_strike),
                "expiry_symbol": self._universe.expiry_symbol,
                "feed_mode": self._feed_mode,
            }
        )

        await self.orchestrator.initialize()
        scan_task = asyncio.create_task(self.orchestrator.run_periodic_scan())
        poll_task = asyncio.create_task(self._run_rest_poll_loop())
        feed_task = asyncio.create_task(self._run_market_feed_loop())
        self._tasks.extend([scan_task, poll_task, feed_task])

        logger.info("trading_engine_ready", instruments=len(self._universe.instruments))

    async def _ensure_session(self) -> bool:
        # Delta public market data works without keys (paper). Live needs API keys.
        if self.config.is_live and not self._has_api_credentials():
            logger.warning(
                "delta_api_key_required",
                hint="Set DELTA_API_KEY and DELTA_API_SECRET in .env",
            )
            await self.journal.write_notification(
                "system",
                "warning",
                "Delta API key missing",
                "Set DELTA_API_KEY and DELTA_API_SECRET in .env (whitelist your IP).",
            )
            set_engine_state({"status": "standby"})
            return False
        if not self._has_api_credentials():
            logger.info("delta_public_mode", hint="Paper trading on public Delta REST/WS")
        return True

    async def reauthenticate(self, *, force: bool = True) -> dict[str, Any]:
        del force
        if not self._has_api_credentials():
            raise RuntimeError("Delta API credentials are not configured")

        was_subscribed = self._ws_started
        if was_subscribed:
            await self._stop_subscription()

        await self.broker.connect()
        set_engine_state({"broker_connected": True, "status": "running"})

        if self._universe is None:
            await self._setup_market_data()
        elif self._is_market_open():
            await self._start_subscription()

        session = resolve_session(self.config.env) or {}
        return {
            "ok": True,
            "user_id": "delta",
            "expires_at": None,
            "valid": True,
            "env": session.get("env"),
            "broker_connected": True,
        }

    def get_underlying_chart_bars(
        self,
        underlying: str,
        *,
        minutes: int = 5,
    ) -> dict[str, Any]:
        """OHLC bars for BTC perp dashboard chart from in-memory session candles."""
        from algocrypto.models.events import CandleInterval

        interval_map = {
            1: (CandleInterval.M1, "1m"),
            3: (CandleInterval.M3, "3m"),
            5: (CandleInterval.M5, "5m"),
        }
        source_interval, interval_label = interval_map.get(
            minutes, (CandleInterval.M5, "5m"),
        )
        candles = self.market_data.candles(source_interval)
        bars: list[dict[str, Any]] = []
        for candle in candles:
            bars.append(
                {
                    "ts": candle.ts.isoformat(),
                    "open": float(candle.open),
                    "high": float(candle.high),
                    "low": float(candle.low),
                    "close": float(candle.close),
                    "volume": candle.volume,
                }
            )
        return {
            "underlying": underlying.upper(),
            "interval": interval_label,
            "price_source": "delta_perp",
            "instrument_token": None,
            "fut_tsym": None,
            "bars": bars,
        }

    async def get_instrument_chart_bars(
        self,
        token: str,
        *,
        exchange: str = "DELTA",
        tsym: str | None = None,
        underlying: str = "BTC",
        minutes: int = 5,
        days: int = 30,
    ) -> dict[str, Any]:
        """OHLC bars for a single option contract (Delta product symbol)."""
        from algocrypto.market_data.engine import session_start_utc
        from algocrypto.models.events import Candle, CandleInterval

        tok = str(token).strip()
        sym = (underlying or "BTC").upper()
        exch = (exchange or "DELTA").strip().upper() or "DELTA"
        interval_map = {
            1: (CandleInterval.M1, "1m"),
            3: (CandleInterval.M3, "3m"),
            5: (CandleInterval.M5, "5m"),
        }
        source_interval, interval_label = interval_map.get(
            minutes, (CandleInterval.M5, "5m"),
        )
        display_tsym = (tsym or "").strip() or None
        api_symbol = display_tsym or tok
        if self._universe:
            match = next(
                (i for i in self._universe.instruments if i.token == tok),
                None,
            )
            if match:
                api_symbol = match.tsym or match.token
                if not display_tsym:
                    display_tsym = match.tsym

        empty: dict[str, Any] = {
            "underlying": sym,
            "interval": interval_label,
            "price_source": "delta_option",
            "instrument_token": tok,
            "fut_tsym": display_tsym or api_symbol,
            "bars": [],
        }
        if not tok:
            return empty

        now = datetime.now(tz=timezone.utc)
        history_start = now - timedelta(days=max(1, days))
        session_start = session_start_utc(now)

        db_bars: list[Candle] = []
        try:
            db_bars = await self.market_data.candles_from_db(
                tok, source_interval, history_start, now
            )
        except Exception:
            logger.exception("option_chart_db_load_failed", token=tok)

        bars_source = self.market_data.merge_candles(db_bars)
        has_recent = bool(bars_source and bars_source[-1].ts >= session_start)
        if not bars_source or not has_recent:
            try:
                broker_bars = await self.broker.get_candles(
                    exch,
                    api_symbol,
                    source_interval,
                    history_start if not bars_source else session_start,
                    now,
                )
                bars_source = self.market_data.merge_candles(bars_source, broker_bars)
            except Exception:
                logger.exception(
                    "option_chart_candle_fetch_failed",
                    token=tok,
                    symbol=api_symbol,
                    interval=interval_label,
                )

        live_ltp: float | None = None
        state = self.option_data.get(tok)
        if state is not None and state.ltp is not None:
            live_ltp = float(state.ltp)
        bars = bars_source
        if live_ltp is not None and bars and bars[-1].ts >= session_start:
            px = Decimal(str(live_ltp))
            last = bars[-1]
            bars = [
                *bars[:-1],
                last.model_copy(
                    update={
                        "high": max(last.high, px),
                        "low": min(last.low, px),
                        "close": px,
                    }
                ),
            ]

        return {
            "underlying": sym,
            "interval": interval_label,
            "price_source": "delta_option",
            "instrument_token": tok,
            "fut_tsym": display_tsym or api_symbol,
            "bars": [
                {
                    "ts": c.ts.isoformat(),
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": c.volume,
                }
                for c in bars
            ],
        }

    def get_chart_bars(
        self,
        underlying: str,
        *,
        minutes: int = 5,
        token: str | None = None,
        exchange: str | None = None,
        tsym: str | None = None,
    ) -> dict[str, Any]:
        """Sync wrapper for underlying chart only (legacy)."""
        del exchange, tsym, token
        return self.get_underlying_chart_bars(underlying, minutes=minutes)

    def get_watchlist_snapshot(self) -> dict[str, Any]:
        from algocrypto.contract_selector.expiry import parse_expiry_tag
        from algocrypto.option_data.greeks import compute_greeks
        from algocrypto.symbols_util import primary_underlying, strike_band_points, strike_step

        underlying = primary_underlying(self.config.symbols)
        spot = self.market_data.spot_ltp
        atm_strike = self._universe.atm_strike if self._universe else None
        step = strike_step(self.config.symbols, underlying)
        band = strike_band_points(self.config.symbols, underlying)
        rate = float(self.config.data_availability.get("risk_free_rate", 0.065))
        expiry_date = None
        if self._universe and self._universe.expiry_symbol:
            expiry_date = parse_expiry_tag(self._universe.expiry_symbol)
        items: list[dict[str, Any]] = []

        if self._universe and atm_strike is not None and spot is not None:
            atm_f = float(atm_strike)
            spot_f = float(spot)
            for inst in sorted(
                self._universe.instruments,
                key=lambda i: (i.strike, 0 if i.option_type == "CE" else 1),
            ):
                strike_f = float(inst.strike)
                if abs(strike_f - atm_f) > band + 1e-9:
                    continue
                steps = round((strike_f - atm_f) / step)
                if abs(steps * step - (strike_f - atm_f)) > 1e-6:
                    continue
                state = self.option_data.get(inst.token)
                lot_size = int(inst.lot_size)
                contract_size = float(getattr(inst, "contract_size", Decimal("0.001")))
                ltp = float(state.ltp) if state and state.ltp is not None else None
                greeks = compute_greeks(
                    spot=spot_f,
                    strike=strike_f,
                    premium=ltp,
                    option_type=inst.option_type,
                    expiry=expiry_date,
                    rate=rate,
                )
                items.append(
                    {
                        "token": inst.token,
                        "tsym": inst.tsym,
                        "symbol": inst.tsym,
                        "strike": strike_f,
                        "option_type": inst.option_type,
                        "underlying": inst.underlying,
                        "is_atm": abs(strike_f - atm_f) < 1e-9,
                        "tradable": abs(strike_f - atm_f) <= step + 1e-9,
                        "lot_size": lot_size,
                        "contract_size": contract_size,
                        "ltp": ltp,
                        "bid": float(state.bid) if state and state.bid is not None else None,
                        "ask": float(state.ask) if state and state.ask is not None else None,
                        "volume": state.volume if state else None,
                        "oi": state.oi if state else None,
                        "iv": round(greeks.iv * 100, 2) if greeks.iv is not None else None,
                        "delta": round(greeks.delta, 4) if greeks.delta is not None else None,
                        "gamma": round(greeks.gamma, 6) if greeks.gamma is not None else None,
                        "theta": round(greeks.theta, 2) if greeks.theta is not None else None,
                        "vega": round(greeks.vega, 2) if greeks.vega is not None else None,
                        "greeks_source": "black_scholes",
                        "last_update_ts": state.last_update_ts.isoformat()
                        if state and state.last_update_ts
                        else None,
                    }
                )
        elif self._universe and atm_strike is not None:
            # Spot missing — still return quotes without Greeks
            atm_f = float(atm_strike)
            for inst in sorted(
                self._universe.instruments,
                key=lambda i: (i.strike, 0 if i.option_type == "CE" else 1),
            ):
                strike_f = float(inst.strike)
                if abs(strike_f - atm_f) > band + 1e-9:
                    continue
                steps = round((strike_f - atm_f) / step)
                if abs(steps * step - (strike_f - atm_f)) > 1e-6:
                    continue
                state = self.option_data.get(inst.token)
                items.append(
                    {
                        "token": inst.token,
                        "tsym": inst.tsym,
                        "strike": strike_f,
                        "option_type": inst.option_type,
                        "is_atm": abs(strike_f - atm_f) < 1e-9,
                        "tradable": abs(strike_f - atm_f) <= step + 1e-9,
                        "lot_size": int(inst.lot_size),
                        "ltp": float(state.ltp) if state and state.ltp is not None else None,
                        "bid": float(state.bid) if state and state.bid is not None else None,
                        "ask": float(state.ask) if state and state.ask is not None else None,
                        "volume": state.volume if state else None,
                        "oi": state.oi if state else None,
                        "iv": None,
                        "delta": None,
                        "gamma": None,
                        "theta": None,
                        "vega": None,
                        "greeks_source": "black_scholes",
                        "last_update_ts": state.last_update_ts.isoformat()
                        if state and state.last_update_ts
                        else None,
                    }
                )

        open_positions = [
            self.orchestrator.positions.serialize_open_position(
                p,
                option_data=self.option_data,
                spot=spot,
            )
            for p in self.orchestrator.positions.open_positions
        ]

        return {
            "underlying": underlying,
            "spot_ltp": float(spot) if spot is not None else None,
            "atm_strike": float(atm_strike) if atm_strike is not None else None,
            "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
            "instrument_count": len(items),
            "strike_count": len({i["strike"] for i in items}),
            "strike_band_points": band,
            "strike_step": step,
            "last_quote_ts": get_engine_state().get("last_quote_ts"),
            "feed_mode": self._feed_mode,
            "ws_open": bool(getattr(self.broker, "websocket_open", False)),
            "market_open": self._is_market_open(),
            "greeks_source": "black_scholes",
            "items": items,
            "open_positions": open_positions,
        }

    async def get_trade_blotter(self, limit: int = 20) -> list[dict[str, Any]]:
        """Today's closed trades with entry/exit time, LTP, lots, P&L."""
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    ct.id,
                    ct.entry_ts,
                    ct.exit_ts,
                    ct.entry_price,
                    ct.exit_price,
                    ct.quantity,
                    ct.pnl,
                    ct.gross_pnl,
                    ct.fees_usd,
                    ct.entry_fee_usd,
                    ct.exit_fee_usd,
                    ct.exit_reason,
                    ct.setup_type,
                    ct.hold_seconds,
                    p.tsym,
                    p.side AS position_side
                FROM closed_trades ct
                JOIN positions p ON p.id = ct.position_id
                WHERE (ct.entry_ts AT TIME ZONE 'Asia/Kolkata')::date
                      = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                ORDER BY ct.exit_ts DESC
                LIMIT $1
                """,
                limit,
            )
        blotter = []
        lot_default = 1
        for r in rows:
            qty = int(r["quantity"])
            lot_size = lot_default
            contract_size = Decimal("0.001")
            if self._universe:
                match = next((i for i in self._universe.instruments if i.tsym == r["tsym"]), None)
                if match:
                    lot_size = match.lot_size
                    contract_size = Decimal(str(match.contract_size))
            lots = qty  # qty stored as Delta lots
            gross = r["gross_pnl"]
            fees = r["fees_usd"]
            blotter.append(
                {
                    "id": str(r["id"]),
                    "entry_ts": r["entry_ts"].isoformat() if r["entry_ts"] else None,
                    "exit_ts": r["exit_ts"].isoformat() if r["exit_ts"] else None,
                    "tsym": r["tsym"],
                    "side": r["position_side"],
                    "entry_price": float(r["entry_price"]),
                    "exit_price": float(r["exit_price"]),
                    "quantity": qty,
                    "lot_size": lot_size,
                    "lots": lots,
                    "contract_size": float(contract_size),
                    "underlying_qty": float(Decimal(lots) * contract_size),
                    "pnl": float(r["pnl"]),
                    "gross_pnl": float(gross) if gross is not None else float(r["pnl"]),
                    "fees_usd": float(fees) if fees is not None else 0.0,
                    "entry_fee_usd": float(r["entry_fee_usd"] or 0),
                    "exit_fee_usd": float(r["exit_fee_usd"] or 0),
                    "exit_reason": r["exit_reason"],
                    "setup_type": r["setup_type"],
                    "hold_seconds": r["hold_seconds"],
                }
            )
        return blotter

    async def sync_missing_data(self) -> dict[str, Any]:
        """Fetch from Delta only what DB / in-memory state is missing."""
        from zoneinfo import ZoneInfo

        from algocrypto.symbols_util import index_symbol, strike_band_points, strike_step

        under = (self.config.symbols.get("primary_underlying") or "BTC").upper()
        band = strike_band_points(self.config.symbols, under)
        step = strike_step(self.config.symbols, under)
        expected_strikes = int(round((2 * band) / step)) + 1 if step else 0
        expected_inst = expected_strikes * 2 * len(self.config.symbols.get("underlyings") or ["BTC"])
        spot_token = index_symbol(self.config.symbols)
        pool = get_pool()

        report: dict[str, Any] = {
            "ok": True,
            "universe": {},
            "candles": {},
            "quotes": {},
        }

        async with pool.acquire() as conn:
            before_inst = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM instruments WHERE in_band = TRUE"
                )
                or 0
            )
            m1_before = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_1m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )
            m3_before = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_3m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )
            m5_before = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_5m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )

        need_universe = (
            before_inst < expected_inst
            or self._universe is None
            or not self._universe.instruments
        )
        if need_universe:
            await self._refresh_universe(reason="manual_sync_missing")
            universe_action = "refreshed"
        else:
            universe_action = "ok"

        after_inst = len(self._universe.instruments) if self._universe else 0
        report["universe"] = {
            "action": universe_action,
            "before": before_inst,
            "after": after_inst,
            "expected": expected_inst,
            "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
        }

        await self.market_data.refresh_session_candles(force=True)
        for interval_candles in self.market_data._candles.values():
            await self.journal.write_candles(interval_candles)

        async with pool.acquire() as conn:
            m1_after = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_1m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )
            m3_after = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_3m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )
            m5_after = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_5m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )

        report["candles"] = {
            "m1_in_memory": len(self.market_data.candles(CandleInterval.M1)),
            "m3_in_memory": len(self.market_data.candles(CandleInterval.M3)),
            "m5_in_memory": len(self.market_data.candles(CandleInterval.M5)),
            "m1_added": max(0, m1_after - m1_before),
            "m3_added": max(0, m3_after - m3_before),
            "m5_added": max(0, m5_after - m5_before),
        }

        missing_tokens: list[str] = []
        if self._universe:
            for inst in self._universe.instruments:
                state = self.option_data.get(inst.token)
                if state is None or state.ltp is None:
                    missing_tokens.append(inst.token)

        report["quotes"]["missing_ltp_before"] = len(missing_tokens)
        polled = 0
        if missing_tokens and self._universe:
            by_token = {i.token: i for i in self._universe.instruments}
            async def _one(token: str):
                inst = by_token[token]
                raw = await self.broker.get_quotes(inst.exchange, inst.token)
                from algocrypto.market_data.poller import quote_from_rest

                return quote_from_rest(inst.exchange, inst.token, raw)

            results = await asyncio.gather(
                *[_one(t) for t in missing_tokens],
                return_exceptions=True,
            )
            for q in results:
                if isinstance(q, Exception) or q is None:
                    continue
                await self.market_data.on_quote(q)
                self.option_data.update_from_quote(q)
                polled += 1
        else:
            await self._poll_rest_quotes_once()
            polled = after_inst

        still_missing = 0
        if self._universe:
            for inst in self._universe.instruments:
                state = self.option_data.get(inst.token)
                if state is None or state.ltp is None:
                    still_missing += 1
        report["quotes"]["polled"] = polled
        report["quotes"]["missing_ltp_after"] = still_missing
        report["spot_ltp"] = (
            float(self.market_data.spot_ltp) if self.market_data.spot_ltp is not None else None
        )
        report["ist_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()

        await self.journal.write_system_event(
            SystemEvent(
                event_type="manual_sync",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message="manual_sync_missing_data",
                metadata=report,
            )
        )
        return report

    async def reset_paper_account(self) -> dict[str, Any]:
        """Wipe mock/paper trades and restore capital for a clean session."""
        capital = account_capital_usd(self.config.risk)
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM closed_trades")
                await conn.execute("DELETE FROM positions")
                await conn.execute("DELETE FROM orders")
                await conn.execute("DELETE FROM validation_results")
                await conn.execute("DELETE FROM ml_scores")
                await conn.execute("DELETE FROM candidate_signals")
                # Drop prior-day risk rows so carry-forward does not revive old equity.
                await conn.execute(
                    """
                    DELETE FROM daily_risk_state
                    WHERE trade_date <> (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                    """
                )
                await conn.execute(
                    "DELETE FROM instruments WHERE in_band = FALSE OR expiry_date < CURRENT_DATE"
                )
                await conn.execute(
                    """
                    UPDATE daily_risk_state SET
                        starting_capital = $1,
                        available_capital = $1,
                        deployed_capital = 0,
                        realized_pnl = 0,
                        trade_count = 0,
                        consecutive_losses = 0,
                        kill_switch = FALSE,
                        entries_blocked = FALSE,
                        block_reason = NULL,
                        updated_at = now()
                    WHERE trade_date = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                    """,
                    capital,
                )
                # Ensure today's risk row exists
                await conn.execute(
                    """
                    INSERT INTO daily_risk_state (
                        trade_date, starting_capital, available_capital, deployed_capital,
                        realized_pnl, trade_count, consecutive_losses, kill_switch, entries_blocked
                    ) VALUES (
                        (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date,
                        $1, $1, 0, 0, 0, 0, FALSE, FALSE
                    )
                    ON CONFLICT (trade_date) DO NOTHING
                    """,
                    capital,
                )
                await conn.execute(
                    """
                    INSERT INTO notifications (type, severity, title, message)
                    VALUES ('system', 'info', 'Paper account reset', $1)
                    """,
                    f"Balance restored to ${capital:,.0f} · old trades and expired tokens removed",
                )

        # Clear in-memory open positions + exit cooldowns so trading can resume.
        self.orchestrator.positions._open.clear()
        self.orchestrator.positions.clear_cooldowns()
        self.orchestrator.positions._pending_flips.clear()
        await self._refresh_universe(reason="paper_account_reset")
        # Force a fresh candle pull so the next scan is not on pre-reset bars.
        refreshed = await self.market_data.refresh_session_candles(force=True)
        if refreshed:
            for interval_candles in self.market_data._candles.values():
                await self.journal.write_candles(interval_candles)
        return {
            "ok": True,
            "starting_capital": float(capital),
            "available_capital": float(capital),
            "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
            "instrument_count": len(self._universe.instruments) if self._universe else 0,
            "candles_refreshed": refreshed,
            "m1_count": len(self.market_data.candles(CandleInterval.M1)),
        }

    def get_market_summary(self) -> dict[str, Any]:
        from zoneinfo import ZoneInfo

        from algocrypto.market_session import session_label

        ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        ms = self.config.market_session
        session = session_label(ms)

        m5 = self.market_data.candles(CandleInterval.M5)
        features = self.orchestrator.features.compute()
        bias = features.bias_5m.value.upper()

        vwap = self.market_data.session_vwap_value
        spot = self.market_data.spot_ltp
        spot_vs_vwap: str | None = None
        if spot is not None and vwap is not None:
            if spot > vwap:
                spot_vs_vwap = "ABOVE"
            elif spot < vwap:
                spot_vs_vwap = "BELOW"
            else:
                spot_vs_vwap = "AT"

        decision = self.orchestrator.router.last_decision
        from algocrypto.symbols_util import primary_underlying

        now = datetime.now(tz=timezone.utc)
        state = get_engine_state()
        quote_age_sec: float | None = None
        last_quote = state.get("last_quote_ts")
        if last_quote:
            try:
                qt = datetime.fromisoformat(str(last_quote).replace("Z", "+00:00"))
                if qt.tzinfo is None:
                    qt = qt.replace(tzinfo=timezone.utc)
                quote_age_sec = max(0.0, (now - qt).total_seconds())
            except Exception:
                quote_age_sec = None
        ws_quote_age_sec: float | None = None
        if self._last_ws_quote_ts is not None:
            ws_quote_age_sec = max(0.0, (now - self._last_ws_quote_ts).total_seconds())
        ws_open = bool(getattr(self.broker, "websocket_open", False))
        if not ws_open and self._feed_mode in ("websocket", "ws"):
            if ws_quote_age_sec is not None and ws_quote_age_sec <= 20.0:
                ws_open = True
            elif quote_age_sec is not None and quote_age_sec <= 15.0:
                ws_open = True

        return {
            "underlying": primary_underlying(self.config.symbols),
            "spot_ltp": float(spot) if spot is not None else None,
            "session_vwap": float(vwap) if vwap is not None else None,
            "spot_vs_vwap": spot_vs_vwap,
            "atm_strike": float(self._universe.atm_strike)
            if self._universe
            else None,
            "bias_5m": bias,
            "market_session": session,
            "market_open": session == "OPEN",
            "strategy": (
                decision.selected_strategy
                if decision
                else self.config.strategy.get("active_scanner", "router")
            ),
            "regime": decision.regime.primary if decision and decision.regime else None,
            "confidence": decision.confidence if decision else None,
            "router_confidence": decision.confidence if decision else None,
            "trading_mode": self.config.env.trading_mode,
            "ist_time": ist.isoformat(),
            "feed_mode": self._feed_mode,
            "ws_open": ws_open,
            "ws_quote_age_sec": ws_quote_age_sec,
            "quote_age_sec": quote_age_sec,
            "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
            "instrument_count": len(self._universe.instruments) if self._universe else 0,
            "m5_count": len(m5),
        }

    async def start(self) -> None:
        setup_logging(self.config.env.log_level, self.config.logging.get("format", "json"))
        await init_pool()
        await apply_migrations()
        from algocrypto.symbols_util import account_capital_usd

        capital = float(account_capital_usd(self.config.risk))
        await ensure_paper_account(capital)
        set_engine_state({"status": "running", "broker_connected": False})

        await self.journal.write_system_event(
            SystemEvent(
                event_type="SYSTEM_START",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message="Trading engine started",
                metadata={"mode": self.config.env.trading_mode},
            )
        )

        if not self._has_api_credentials():
            logger.warning(
                "broker_api_key_missing",
                hint="Set DELTA_API_KEY and DELTA_API_SECRET in .env (optional for paper public data)",
            )
            if self.config.is_live:
                await self.journal.write_notification(
                    "system",
                    "warning",
                    "Delta API key missing",
                    "Set DELTA_API_KEY and DELTA_API_SECRET in .env (whitelist your IP).",
                )
                set_engine_state({"status": "standby"})
                return

        if not await self._ensure_session():
            return

        await self.broker.connect()
        set_engine_state({"broker_connected": True})
        await self._setup_market_data()
        # Restore OPEN positions after restart so Used Margin matches holdings.
        try:
            n = await self.orchestrator.positions.rehydrate_open_positions()
            if n:
                logger.info("open_positions_restored", count=n)
        except Exception:
            logger.exception("position_rehydrate_failed")

    async def stop(self) -> None:
        self.orchestrator.stop()
        await self._stop_subscription()
        await close_pool()
        set_engine_state({"status": "stopped", "broker_connected": False})


async def _run() -> None:
    engine = TradingEngineApp()
    set_engine_app(engine)
    config = uvicorn.Config(health_app, host="0.0.0.0", port=8001, log_level="info")
    server = uvicorn.Server(config)
    api_task = asyncio.create_task(server.serve())

    try:
        await engine.start()
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await engine.stop()
        server.should_exit = True
        await api_task


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("shutdown_signal_received")


if __name__ == "__main__":
    main()
