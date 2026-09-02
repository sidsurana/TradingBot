import json
from decimal import Decimal

import httpx
import pytest

from tradingbot.config import PolymarketUSCreds
from tradingbot.exchanges.polymarket_us import (
    PolymarketUSExchange,
    parse_polymarket_us_markets,
)
from tradingbot.exchanges.polymarket_us_auth import auth_headers, load_signer
from tradingbot.models import Market, Order, OrderType, Side, Venue


def _m(slug, question, yes_price, n_hint="2026-11-01"):
    return {"slug": slug, "question": question, "title": slug.split("-")[-1].upper(),
            "endDate": n_hint, "category": "sports",
            "outcomePrices": [str(yes_price), str(round(1 - yes_price, 4))],
            "orderPriceMinTickSize": "0.01", "minimumTradeQty": "1", "volume24hr": "100"}


def test_parse_exhaustive_group_gets_distinct_outcomes_and_count():
    # 3-way complete field whose fair YES prices sum ~1 -> dutch-book eligible.
    data = [_m("champ-a", "Champ", 0.5), _m("champ-b", "Champ", 0.3),
            _m("champ-c", "Champ", 0.2)]
    legs = parse_polymarket_us_markets(data, Venue.POLYMARKET)
    assert len({m.outcome for m in legs}) == 3          # distinct outcomes
    assert all(m.metadata["num_outcomes"] == 3 for m in legs)
    assert all(m.event_id == "Champ|2026-11-01" for m in legs)


def test_parse_incomplete_field_not_dutch_bookable():
    # Fair YES prices sum far below 1 -> missing outcomes -> NOT a locked set.
    data = [_m("mvp-a", "MVP", 0.10), _m("mvp-b", "MVP", 0.07)]
    legs = parse_polymarket_us_markets(data, Venue.POLYMARKET)
    assert {m.outcome for m in legs} == {"YES"}
    assert all("num_outcomes" not in m.metadata for m in legs)


@pytest.mark.asyncio
async def test_fetch_order_book_parses_px_qty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"marketData": {"marketSlug": "s",
            "bids": [{"px": {"value": "0.48", "currency": "USD"}, "qty": "100.0"}],
            "offers": [{"px": {"value": "0.52", "currency": "USD"}, "qty": "80.0"}]}})

    ex = PolymarketUSExchange(PolymarketUSCreds(key_id="k", secret_key="c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0MzI="))
    ex._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t")
    ex._sign = lambda m, p: {}
    mkt = Market(venue=Venue.POLYMARKET, market_id="s", event_id="e", title="t", outcome="YES")
    book = await ex.fetch_order_book(mkt)
    await ex._client.aclose()
    assert book.best_bid.price == 0.48 and book.best_bid.size == Decimal("100.0")
    assert book.best_ask.price == 0.52 and book.best_ask.size == Decimal("80.0")


@pytest.mark.asyncio
async def test_place_order_builds_us_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": "ord123", "executions": []})

    ex = PolymarketUSExchange(PolymarketUSCreds(key_id="k", secret_key="c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0MzI="))
    ex._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t")
    ex._sign = lambda m, p: {}
    mkt = Market(venue=Venue.POLYMARKET, market_id="champ-a", event_id="e", title="t",
                 outcome="A", tick_size=0.01)
    order = Order(market=mkt, side=Side.BUY, size=Decimal(5), type=OrderType.LIMIT, price=0.37)
    res = await ex.place_order(order)
    await ex._client.aclose()
    assert seen["path"] == "/v1/orders"
    assert seen["body"]["marketSlug"] == "champ-a"
    assert seen["body"]["action"] == "ORDER_ACTION_BUY"
    assert seen["body"]["outcomeSide"] == "OUTCOME_SIDE_YES"
    assert seen["body"]["price"]["value"].startswith("0.37")
    assert res.venue_id == "ord123"


def test_auth_headers_are_deterministic_and_signed():
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    secret = base64.b64encode(bytes(range(32))).decode()  # 32-byte ed25519 seed
    signer = load_signer("key-1", secret)
    h = signer("GET", "/v1/portfolio/positions")
    assert h["X-PM-Access-Key"] == "key-1"
    assert h["X-PM-Timestamp"] and h["X-PM-Signature"]
    # same inputs + fixed timestamp -> identical signature (deterministic ed25519)
    sk = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret)[:32])
    h1 = auth_headers("key-1", sk, "GET", "/p", timestamp_ms=1000)
    h2 = auth_headers("key-1", sk, "GET", "/p", timestamp_ms=1000)
    assert h1["X-PM-Signature"] == h2["X-PM-Signature"]
