"""Kalshi request signing.

Kalshi's trading API authenticates each request with an RSA signature: you sign
the string `timestamp + METHOD + path` with your account's RSA private key
(RSA-PSS / SHA-256) and send the signature, your key id, and the timestamp as
headers. See https://trading-api.readme.io/ → API Keys.

Kept separate from the adapter so the signing is unit-testable without network.
Requires the `cryptography` package (the `live` extra).
"""

from __future__ import annotations

import base64
import time
from functools import lru_cache


@lru_cache(maxsize=4)
def _load_private_key(path: str):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    with open(path, "rb") as f:
        return load_pem_private_key(f.read(), password=None)


def sign_message(private_key, message: str) -> str:
    """RSA-PSS / SHA-256 signature of `message`, base64-encoded."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def auth_headers(key_id: str, private_key, method: str, path: str,
                 timestamp_ms: int | None = None) -> dict[str, str]:
    """Build the three Kalshi auth headers for a request.

    `path` is the URL path WITHOUT the query string (e.g.
    "/trade-api/v2/portfolio/orders"). `method` is upper-case (GET/POST/DELETE).
    """
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    message = ts + method.upper() + path
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": sign_message(private_key, message),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


def load_signer(key_id: str, private_key_path: str):
    """Return a callable (method, path) -> headers, with the key loaded once."""
    private_key = _load_private_key(private_key_path)

    def _signer(method: str, path: str) -> dict[str, str]:
        return auth_headers(key_id, private_key, method, path)

    return _signer
