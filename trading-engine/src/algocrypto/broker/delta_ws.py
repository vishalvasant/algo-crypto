"""Delta Exchange public/private WebSocket feed."""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import structlog
import websocket

from algocrypto.broker.auth import now_timestamp, sign_request

logger = structlog.get_logger(__name__)


class DeltaMarketSocket:
    def __init__(
        self,
        *,
        ws_url: str,
        api_key: str | None,
        api_secret: str | None,
        on_quote: Callable[[dict[str, Any]], None] | None = None,
        on_open: Callable[[], None] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._on_quote = on_quote
        self._on_open = on_open
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._connected = False
        self._lock = threading.Lock()
        self._subscribed: list[str] = []

    @property
    def is_open(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._ws = websocket.WebSocketApp(
            self._ws_url,
            on_open=self._handle_open,
            on_message=self._handle_message,
            on_error=self._handle_error,
            on_close=self._handle_close,
        )
        self._thread = threading.Thread(target=self._run, name="delta-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self._connected = False

    def subscribe(self, symbols: list[str]) -> None:
        with self._lock:
            self._subscribed = list(dict.fromkeys(symbols))
        if self.is_open and self._subscribed:
            self._send_subscribe(self._subscribed)

    def _run(self) -> None:
        assert self._ws is not None
        while self._running:
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logger.warning("delta_ws_run_exception", error=str(exc))
            self._connected = False
            if not self._running:
                break
            time.sleep(2)

    def _handle_open(self, _ws: Any) -> None:
        self._connected = True
        # Optional private auth
        if self._api_key and self._api_secret:
            ts, sig = sign_request(
                self._api_secret,
                "GET",
                "/live",
                timestamp=now_timestamp(),
            )
            auth_msg = {
                "type": "key-auth",
                "payload": {
                    "api-key": self._api_key,
                    "signature": sig,
                    "timestamp": ts,
                },
            }
            try:
                assert self._ws is not None
                self._ws.send(json.dumps(auth_msg))
            except Exception as exc:
                logger.warning("delta_ws_auth_failed", error=str(exc))
        with self._lock:
            symbols = list(self._subscribed)
        if symbols:
            self._send_subscribe(symbols)
        if self._on_open:
            self._on_open()
        logger.info("delta_ws_open", symbols=len(symbols))

    def _send_subscribe(self, symbols: list[str]) -> None:
        if not self._ws:
            return
        # Public v2/ticker channel for options + mark
        payload = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {
                        "name": "v2/ticker",
                        "symbols": symbols[:200],
                    }
                ]
            },
        }
        try:
            self._ws.send(json.dumps(payload))
            logger.info("delta_ws_subscribed", count=len(symbols[:200]))
        except Exception:
            logger.exception("delta_ws_subscribe_failed")

    def _handle_message(self, _ws: Any, message: str) -> None:
        try:
            data = json.loads(message)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        msg_type = data.get("type") or data.get("t")
        if msg_type in ("heartbeat", "ping", "pong", "key-auth"):
            return
        # ticker payloads often under "result" or flat
        payload = data.get("result") or data.get("payload") or data
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and self._on_quote:
                    self._on_quote(item)
            return
        if isinstance(payload, dict) and (
            payload.get("symbol") or payload.get("product_symbol") or payload.get("mark_price")
        ):
            if self._on_quote:
                self._on_quote(payload)

    def _handle_error(self, _ws: Any, error: Any) -> None:
        logger.warning("delta_ws_error", error=str(error))

    def _handle_close(self, _ws: Any, status: Any, msg: Any) -> None:
        self._connected = False
        logger.info("delta_ws_closed", status=status, msg=str(msg) if msg else None)
