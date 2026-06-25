"""Kalshi RSA signing — verify the signature is valid for the signed message."""

import base64

import pytest

crypto = pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from tradingbot.exchanges.kalshi_auth import auth_headers, sign_message  # noqa: E402


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_signature_verifies():
    priv = _key()
    msg = "1700000000000POST/trade-api/v2/portfolio/orders"
    sig_b64 = sign_message(priv, msg)
    # The public key must verify the signature over the exact message bytes.
    priv.public_key().verify(
        base64.b64decode(sig_b64),
        msg.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )  # raises InvalidSignature on failure


def test_auth_headers_shape_and_message():
    priv = _key()
    headers = auth_headers("my-key-id", priv, "post",
                           "/trade-api/v2/portfolio/orders", timestamp_ms=1700000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "my-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    # Signature is over "<ts>POST<path>" — method upper-cased.
    priv.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000POST/trade-api/v2/portfolio/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_tampered_message_fails():
    priv = _key()
    sig_b64 = sign_message(priv, "original")
    with pytest.raises(Exception):
        priv.public_key().verify(
            base64.b64decode(sig_b64),
            b"tampered",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
