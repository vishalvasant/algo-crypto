from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from algocrypto.broker.base import BrokerAdapter
from algocrypto.config import AppConfig
from algocrypto.models.events import Candle, CandleInterval, ExecutionRequest, OrderUpdate, QuoteUpdate, TradingMode

logger = structlog.get_logger(__name__)


class PaperBrokerAdapter(BrokerAdapter):
    """Uses a real data adapter for quotes; simulates fills at LTP."""

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

    async def place_order(self, request: ExecutionRequest) -> OrderUpdate:
        fill_price = request.reference_ltp
        # Prefer L2 top-of-book: buys lift the ask, sells hit the bid.
        book_fn = getattr(self._data, "get_l2_orderbook", None)
        if callable(book_fn):
            try:
                depth = int((self._config.fees.get("orderbook") or {}).get("depth", 5))
                raw = await book_fn(request.instrument_token, depth)
                from algocrypto.fees import parse_l2_orderbook

                book = parse_l2_orderbook(request.instrument_token, raw or {})
                if request.side.upper() == "BUY" and book.best_ask is not None:
                    fill_price = book.best_ask
                elif request.side.upper() == "SELL" and book.best_bid is not None:
                    fill_price = book.best_bid
            except Exception:
                logger.exception("paper_orderbook_fill_failed", tsym=request.tsym)

        if fill_price is None or fill_price <= 0:
            if request.instrument_token in self._ltp_cache:
                fill_price = self._ltp_cache[request.instrument_token]
            else:
                fill_price = request.reference_ltp

        now = datetime.now(tz=timezone.utc)
        ref = request.reference_ltp or fill_price
        slippage = fill_price - ref
        logger.info(
            "paper_order_filled",
            client_order_id=request.client_order_id,
            side=request.side,
            fill_price=str(fill_price),
            slippage=str(slippage),
        )
        return OrderUpdate(
            ts=now,
            client_order_id=request.client_order_id,
            broker_order_id=f"PAPER-{request.client_order_id[-8:]}",
            status="COMPLETE",
            report_type="Fill",
            fill_price=fill_price,
            filled_qty=request.quantity,
            avg_price=fill_price,
            slippage=slippage,
            latency_ms=self._config.paper_trading.get("simulate_latency_ms", 0),
            mode=TradingMode.PAPER,
        )
