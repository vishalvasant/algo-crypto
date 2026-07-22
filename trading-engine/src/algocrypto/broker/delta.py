"""Delta Exchange India REST + WebSocket market data adapter."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from algocrypto.broker.auth import auth_headers, has_api_credentials
from algocrypto.broker.base import BrokerAdapter
from algocrypto.broker.delta_ws import DeltaMarketSocket
from algocrypto.config import AppConfig
from algocrypto.models.events import (
    Candle,
    CandleInterval,
    ExecutionRequest,
    OrderUpdate,
)

logger = structlog.get_logger(__name__)

_INTERVAL_MAP = {
    CandleInterval.M1: "1m",
    CandleInterval.M3: "3m",
    CandleInterval.M5: "5m",
}


class DeltaAdapter(BrokerAdapter):
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        base = str(config.broker.get("api_base_url", "https://api.india.delta.exchange"))
        self._base = base.rstrip("/")
        self._timeout = float(config.broker.get("request_timeout_seconds", 20))
        self._client: httpx.AsyncClient | None = None
        self._connected = False
        self._market_socket: DeltaMarketSocket | None = None
        self._quote_callback: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = asyncio.Lock()
        self._ltp_cache: dict[str, Decimal] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def websocket_open(self) -> bool:
        return bool(self._market_socket and self._market_socket.is_open)

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._ensure_client()
        # Public ping
        await self._get("/v2/products", params={"contract_types": "perpetual_futures", "page_size": "1"})
        self._connected = True
        logger.info("delta_connected", base=self._base, private=has_api_credentials(self._config.env))

    async def _ensure_client(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                timeout=self._timeout,
                headers={"User-Agent": "algo-crypto/0.1", "Accept": "application/json"},
            )

    async def stop_websocket(self) -> None:
        """Stop market WS only — keep REST client for candles/quotes."""
        if self._market_socket is not None:
            await asyncio.to_thread(self._market_socket.stop)
            self._market_socket = None
            logger.info("delta_websocket_stopped")

    async def disconnect(self) -> None:
        await self.stop_websocket()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        private: bool = False,
    ) -> Any:
        await self._ensure_client()
        assert self._client is not None
        query = ""
        if params:
            query = "?" + urlencode({k: str(v) for k, v in params.items() if v is not None})
        headers: dict[str, str] = {}
        if private:
            headers = auth_headers(self._config.env, "GET", path, query=query)
        try:
            resp = await self._client.get(path, params=params, headers=headers)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "delta_http_failed",
                path=path,
                params=params,
                error=repr(exc),
            )
            raise
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(payload.get("error") or payload)
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    async def get_products(
        self,
        *,
        contract_types: str,
        underlying: str | None = None,
        expiry: str | None = None,
        states: str = "live",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "contract_types": contract_types,
            "states": states,
            "page_size": "100",
        }
        if underlying:
            # products filter varies — also filter client-side
            pass
        if expiry:
            params["expiry"] = expiry  # YYYY-MM-DD
        rows = await self._get("/v2/products", params=params)
        if not isinstance(rows, list):
            return []
        if underlying:
            u = underlying.upper()
            rows = [
                r
                for r in rows
                if str(r.get("underlying_asset", {}).get("symbol", "")).upper() == u
                or str(r.get("symbol", "")).upper().find(f"-{u}-") >= 0
            ]
        return rows

    async def get_option_tickers(
        self,
        *,
        underlying: str,
        expiry_date: str,
    ) -> list[dict[str, Any]]:
        """expiry_date: DD-MM-YYYY (Delta option-chain convention)."""
        params = {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": underlying.upper(),
            "expiry_date": expiry_date,
        }
        rows = await self._get("/v2/tickers", params=params)
        return rows if isinstance(rows, list) else []

    async def get_index_ticker(self, symbol: str) -> dict[str, Any]:
        # Indices via /v2/tickers or /v2/indices — try ticker by symbol
        try:
            row = await self._get(f"/v2/tickers/{symbol}")
            return row if isinstance(row, dict) else {}
        except Exception:
            indices = await self._get("/v2/indices")
            if isinstance(indices, list):
                for ix in indices:
                    if str(ix.get("symbol")) == symbol:
                        return ix
            return {}

    async def get_candles(
        self,
        exchange: str,
        token: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        del exchange
        # token here is product symbol e.g. BTCUSD or C-BTC-...
        resolution = _INTERVAL_MAP[interval]
        params = {
            "resolution": resolution,
            "symbol": token,
            "start": str(int(start.timestamp())),
            "end": str(int(end.timestamp())),
        }
        try:
            rows = await self._get("/v2/history/candles", params=params)
        except Exception as exc:
            logger.warning("delta_candles_failed", symbol=token, error=repr(exc))
            return []
        if not isinstance(rows, list):
            return []
        out: list[Candle] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts_raw = row.get("time") or row.get("timestamp")
            if ts_raw is None:
                continue
            ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
            out.append(
                Candle(
                    instrument_token=token,
                    ts=ts,
                    open=Decimal(str(row.get("open", 0))),
                    high=Decimal(str(row.get("high", 0))),
                    low=Decimal(str(row.get("low", 0))),
                    close=Decimal(str(row.get("close", 0))),
                    volume=int(float(row.get("volume", 0) or 0)) or None,
                    interval=interval,
                )
            )
        out.sort(key=lambda c: c.ts)
        return out

    async def get_quotes(self, exchange: str, token: str) -> dict[str, Any]:
        del exchange
        # token is product symbol
        try:
            row = await self._get(f"/v2/tickers/{token}")
            if isinstance(row, dict):
                mark = row.get("mark_price") or row.get("close") or row.get("spot_price")
                if mark is not None:
                    self._ltp_cache[token] = Decimal(str(mark))
                return {
                    "lp": mark,
                    "bp1": (row.get("quotes") or {}).get("bid") if isinstance(row.get("quotes"), dict) else row.get("bid"),
                    "sp1": (row.get("quotes") or {}).get("ask") if isinstance(row.get("quotes"), dict) else row.get("ask"),
                    "v": row.get("volume"),
                    "oi": row.get("oi"),
                    "raw": row,
                }
        except Exception as exc:
            logger.warning("delta_quote_failed", symbol=token, error=str(exc))
        return {}

    async def get_l2_orderbook(self, symbol: str, depth: int = 10) -> dict[str, Any]:
        """L2 book: GET /v2/l2orderbook/{symbol}?depth=N — buy/sell levels with price+size."""
        try:
            row = await self._get(
                f"/v2/l2orderbook/{symbol}",
                params={"depth": max(1, min(int(depth), 50))},
            )
            return row if isinstance(row, dict) else {}
        except Exception as exc:
            logger.warning("delta_l2_orderbook_failed", symbol=symbol, error=str(exc))
            return {}

    async def get_option_chain(
        self,
        exchange: str,
        tradingsymbol: str,
        strikeprice: float,
        count: int,
    ) -> list[dict[str, Any]]:
        """Compatibility shim — prefer get_option_tickers in crypto selector."""
        del exchange, strikeprice, count
        # tradingsymbol like BTC:04-04-2025
        if ":" in tradingsymbol:
            underlying, expiry = tradingsymbol.split(":", 1)
            return await self.get_option_tickers(underlying=underlying, expiry_date=expiry)
        return []

    def _handle_feed_update(self, message: dict[str, Any]) -> None:
        if self._quote_callback is None or self._loop is None:
            return
        try:
            from algocrypto.models.events import QuoteUpdate

            symbol = str(message.get("symbol") or message.get("product_symbol") or "")
            ltp = message.get("mark_price") or message.get("close") or message.get("price")
            if ltp is None and isinstance(message.get("quotes"), dict):
                ltp = message["quotes"].get("ask") or message["quotes"].get("bid")
            quote = QuoteUpdate(
                ts=datetime.now(tz=timezone.utc),
                exchange="DELTA",
                instrument_token=symbol,
                tsym=symbol,
                ltp=self.parse_decimal(ltp),
                bid=self.parse_decimal(
                    (message.get("quotes") or {}).get("bid")
                    if isinstance(message.get("quotes"), dict)
                    else message.get("bid")
                ),
                ask=self.parse_decimal(
                    (message.get("quotes") or {}).get("ask")
                    if isinstance(message.get("quotes"), dict)
                    else message.get("ask")
                ),
                volume=int(float(message["volume"])) if message.get("volume") not in (None, "") else None,
                oi=int(float(message["oi"])) if message.get("oi") not in (None, "") else None,
                source="websocket",
            )
            if quote.ltp is not None:
                self._ltp_cache[symbol] = quote.ltp
            self._loop.call_soon_threadsafe(self._quote_callback, quote)
        except Exception:
            logger.exception("delta_feed_parse_failed", message=message)

    async def subscribe(
        self,
        instruments: list[str],
        on_quote: Any,
        on_order: Any | None = None,
    ) -> None:
        del on_order
        async with self._start_lock:
            self._quote_callback = on_quote
            symbols = [i.split("|", 1)[-1] for i in instruments if i]

            if self._market_socket and self._market_socket.is_open:
                await asyncio.to_thread(self._market_socket.subscribe, symbols)
                return

            if self._market_socket is not None:
                await asyncio.to_thread(self._market_socket.stop)
                self._market_socket = None

            ws_url = str(
                self._config.broker.get(
                    "websocket_url", "wss://socket.india.delta.exchange"
                )
            )
            opened = asyncio.Event()

            def _open_cb() -> None:
                self._loop.call_soon_threadsafe(opened.set)  # type: ignore[union-attr]

            sock = DeltaMarketSocket(
                ws_url=ws_url,
                api_key=self._config.env.delta_api_key,
                api_secret=self._config.env.delta_api_secret,
                on_quote=self._handle_feed_update,
                on_open=_open_cb,
            )
            self._market_socket = sock
            sock.subscribe(symbols)
            await asyncio.to_thread(sock.start)
            try:
                await asyncio.wait_for(opened.wait(), timeout=15)
                logger.info("delta_websocket_ready", symbols=len(symbols))
            except asyncio.TimeoutError:
                logger.warning("delta_websocket_open_timeout")

    async def place_order(self, request: ExecutionRequest) -> OrderUpdate:
        raise NotImplementedError("Live Delta orders enabled after paper validation")
