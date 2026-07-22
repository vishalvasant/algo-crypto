"""Delta Exchange API auth (HMAC-SHA256)."""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from algocrypto.config import EnvSettings


def now_timestamp() -> str:
    return str(int(time.time()))


def sign_request(
    secret: str,
    method: str,
    path: str,
    *,
    query: str = "",
    body: str = "",
    timestamp: str | None = None,
) -> tuple[str, str]:
    """Return (timestamp, signature_hex) for Delta private REST/WS."""
    ts = timestamp or now_timestamp()
    # method + timestamp + path + query + body  (docs: string concatenation)
    prehash = f"{method.upper()}{ts}{path}{query}{body}"
    sig = hmac.new(
        secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return ts, sig


def auth_headers(
    env: EnvSettings,
    method: str,
    path: str,
    *,
    query: str = "",
    body: str = "",
) -> dict[str, str]:
    key = env.delta_api_key or ""
    secret = env.delta_api_secret or ""
    if not key or not secret:
        raise ConnectionError("DELTA_API_KEY / DELTA_API_SECRET required")
    ts, sig = sign_request(secret, method, path, query=query, body=body)
    return {
        "api-key": key,
        "timestamp": ts,
        "signature": sig,
        "User-Agent": "algo-crypto/0.1",
        "Content-Type": "application/json",
    }


def has_api_credentials(env: EnvSettings) -> bool:
    return bool(env.delta_api_key and env.delta_api_secret)


def resolve_session(env: EnvSettings) -> dict[str, Any] | None:
    """Delta uses static API keys (no OAuth session file)."""
    if not has_api_credentials(env):
        return None
    return {
        "api_key": env.delta_api_key,
        "valid": True,
        "env": env.delta_env,
    }
