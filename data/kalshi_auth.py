"""Kalshi authenticated-request signing, shared by any script that needs
authenticated REST or WebSocket access (unlike data/kalshi.py, which only
hits Kalshi's unauthenticated public discovery endpoints).

Scheme (confirmed live against Kalshi's docs, 2026-10-04:
https://docs.kalshi.com/getting_started/api_keys and
https://docs.kalshi.com/getting_started/quick_start_websockets):

  Three headers accompany every authenticated request/handshake:
    KALSHI-ACCESS-KEY:       the API key ID
    KALSHI-ACCESS-TIMESTAMP: unix timestamp in MILLISECONDS
    KALSHI-ACCESS-SIGNATURE: base64-encoded signature over
                              "{timestamp}{METHOD}{path}" (path WITHOUT
                              query parameters; for the WebSocket handshake,
                              method is always "GET" and path is the
                              literal string "/trade-api/ws/v2").

  RSA (2048-bit) keys: RSA-PSS, MGF1(SHA-256), salt_length=DIGEST_LENGTH,
  hashed with SHA-256. Ed25519 keys: signed directly (RFC 8032). This
  module supports both, detected from the loaded key's type.

This module ONLY signs requests; it never constructs or sends an order,
and nothing here references any order-placement endpoint.

Credentials are read from environment variables (already present in this
project's .env, loaded via python-dotenv by callers):
  KALSHI_KEY_ID            -- the API key ID (not a secret by itself, but
                               still never logged/printed here)
  KALSHI_PRIVATE_KEY_PATH  -- path (relative to repo root or absolute) to
                               the PEM private key file
  KALSHI_ENV               -- "production" or "demo", selects the base URLs
"""
import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

WS_PATH = "/trade-api/ws/v2"

REST_BASE_URLS = {
    "production": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
WS_BASE_URLS = {
    "production": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
}


class KalshiAuthError(ValueError):
    pass


def load_private_key(key_path: str | Path):
    """Loads a PEM private key (RSA PKCS#1 or Ed25519 PKCS#8). Never logs
    its contents. Raises KalshiAuthError (not a bare exception) if the
    file is missing or unparseable, so callers fail closed rather than
    silently proceeding unauthenticated."""
    path = Path(key_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    if not path.exists():
        raise KalshiAuthError(f"Kalshi private key file not found: {path}")
    try:
        with open(path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    except Exception as exc:
        raise KalshiAuthError(f"Could not parse Kalshi private key at {path}: {exc}") from exc
    if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)):
        raise KalshiAuthError(f"Unsupported Kalshi private key type: {type(key)}")
    return key


def sign_text(private_key, text: str) -> str:
    """Returns the base64-encoded signature over `text`, using RSA-PSS/SHA-256
    for an RSA key or direct Ed25519 signing, matching Kalshi's documented
    scheme exactly."""
    message = text.encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode("utf-8")


def build_auth_headers(key_id: str, private_key, method: str, path: str) -> dict[str, str]:
    """Builds the three KALSHI-ACCESS-* headers for one request/handshake.
    `path` must be the request path WITHOUT query parameters -- the caller
    is responsible for stripping any query string before calling this."""
    timestamp_ms = str(int(time.time() * 1000))
    path_no_query = path.split("?")[0]
    signature = sign_text(private_key, timestamp_ms + method + path_no_query)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


REST_SIGNED_PATH_PREFIX = "/trade-api/v2"


def build_rest_auth_headers(key_id: str, private_key, method: str, sub_path: str) -> dict[str, str]:
    """Builds auth headers for a REST call, given `sub_path` WITHOUT the
    "/trade-api/v2" prefix (e.g. "/portfolio/balance") -- the prefix is
    added automatically before signing, matching what Kalshi actually
    expects signed (the full request path, confirmed live 2026-10-04: a
    bare sub_path alone produces INCORRECT_API_KEY_SIGNATURE). Always use
    this helper for REST calls rather than build_auth_headers() directly,
    to avoid re-introducing that exact mistake."""
    return build_auth_headers(key_id, private_key, method, REST_SIGNED_PATH_PREFIX + sub_path)


def build_ws_auth_headers(key_id: str, private_key) -> dict[str, str]:
    """Builds the auth headers for the WebSocket handshake specifically
    (method is always GET, path is always the literal WS_PATH -- never the
    REST base path)."""
    return build_auth_headers(key_id, private_key, "GET", WS_PATH)


def load_credentials_from_env() -> tuple[str, object, str]:
    """Reads KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH / KALSHI_ENV from the
    process environment (caller must have already called load_dotenv()).
    Returns (key_id, private_key_object, env). Never logs the key ID or
    any part of the private key. Fails closed if anything is missing."""
    import os

    key_id = os.environ.get("KALSHI_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    env = os.environ.get("KALSHI_ENV", "production")
    if not key_id:
        raise KalshiAuthError("KALSHI_KEY_ID is not set in the environment")
    if not key_path:
        raise KalshiAuthError("KALSHI_PRIVATE_KEY_PATH is not set in the environment")
    if env not in REST_BASE_URLS:
        raise KalshiAuthError(f"KALSHI_ENV={env!r} is not one of {list(REST_BASE_URLS)}")
    private_key = load_private_key(key_path)
    return key_id, private_key, env
