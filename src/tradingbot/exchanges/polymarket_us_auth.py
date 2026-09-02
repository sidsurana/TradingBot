"""Polymarket US request signing.

Polymarket US (api.polymarket.us) — a separate, CFTC-regulated platform from
polymarket.com — authenticates each request with an Ed25519 signature. You sign
the string `timestamp + METHOD + path` with your account's secret key and send
the signature, your key id, and the timestamp as headers:

    X-PM-Access-Key   the Key ID (UUID) from the dev portal
    X-PM-Timestamp    unix time in milliseconds (must be within ~30s of server)
    X-PM-Signature    base64( Ed25519_sign(ts + METHOD + path) )

The secret key is a base64 blob; the first 32 bytes are the Ed25519 seed (the
dev portal shows a 64-byte seed+public concatenation). Kept separate from the
adapter so signing is unit-testable without network. Requires `cryptography`.
"""

from __future__ import annotations

import base64
import time
from functools import lru_cache


@lru_cache(maxsize=4)
def _load_signing_key(secret_key_b64: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    seed = base64.b64decode(secret_key_b64)[:32]  # dev portal returns 64B seed+pub
    return Ed25519PrivateKey.from_private_bytes(seed)


def sign_message(signing_key, message: str) -> str:
    """base64-encoded Ed25519 signature of `message`."""
    return base64.b64encode(signing_key.sign(message.encode("utf-8"))).decode("utf-8")


def auth_headers(key_id: str, signing_key, method: str, path: str,
                 timestamp_ms: int | None = None) -> dict[str, str]:
    """Build the Polymarket US auth headers for a request. `path` excludes the
    query string; `method` is upper-case (GET/POST/DELETE)."""
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    message = ts + method.upper() + path
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": ts,
        "X-PM-Signature": sign_message(signing_key, message),
        "Content-Type": "application/json",
    }


def load_signer(key_id: str, secret_key_b64: str):
    """Return a callable (method, path) -> headers, with the key loaded once."""
    signing_key = _load_signing_key(secret_key_b64)

    def _signer(method: str, path: str) -> dict[str, str]:
        return auth_headers(key_id, signing_key, method, path)

    return _signer
