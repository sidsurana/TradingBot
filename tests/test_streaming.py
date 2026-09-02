from decimal import Decimal

import pytest

from tradingbot.config import Settings, StreamingSettings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.exchanges.streaming import (
    LocalBook,
    StreamManager,
    parse_kalshi_message,
    parse_polymarket_message,
    parse_polymarket_us_message,
)
from tradingbot.models import Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


def test_localbook_snapshot_and_topofbook():
    lb = LocalBook("k:1")
    lb.set_snapshot(bids=[(0.40, 10), (0.39, 5)], asks=[(0.42, 8), (0.43, 20)])
    ob = lb.to_order_book()
    assert ob.best_bid.price == 0.40 and ob.best_bid.size == Decimal(10)
    assert ob.best_ask.price == 0.42
    assert round(ob.spread, 2) == 0.02


def test_localbook_additive_delta_removes_at_zero():
    lb = LocalBook("k:1")
    lb.set_snapshot(bids=[(0.40, 10)], asks=[(0.42, 8)])
    lb.apply_delta(is_bid=True, price=0.40, delta=5)     # 10 -> 15
    assert lb.bids[0.40] == Decimal(15)
    lb.apply_delta(is_bid=True, price=0.40, delta=-15)   # -> removed
    assert 0.40 not in lb.bids


def test_localbook_absolute_set_level():
    lb = LocalBook("p:1")
    lb.set_level(is_bid=False, price=0.55, size=12)
    assert lb.asks[0.55] == Decimal(12)
    lb.set_level(is_bid=False, price=0.55, size=0)       # absolute 0 removes
    assert 0.55 not in lb.asks


def test_parse_kalshi_snapshot_folds_no_into_asks():
    books = {}
    key_by_ticker = {"K1": "kalshi:K1"}
    # 2026 wire format: dollar-string prices, fixed-point string sizes.
    # YES bids at $0.40; NO bids at $0.55 -> YES ask at (1-0.55)=$0.45.
    msg = {"type": "orderbook_snapshot",
           "msg": {"market_ticker": "K1",
                   "yes_dollars_fp": [["0.40", "10.00"]],
                   "no_dollars_fp": [["0.55", "7.00"]]}}
    parse_kalshi_message(msg, key_by_ticker, books)
    ob = books["kalshi:K1"].to_order_book()
    assert ob.best_bid.price == 0.40
    assert ob.best_ask.price == 0.45 and ob.best_ask.size == Decimal("7.00")


def test_parse_kalshi_delta_applies_dollar_price_and_fp_size():
    books = {}
    key_by_ticker = {"K1": "kalshi:K1"}
    parse_kalshi_message(
        {"type": "orderbook_snapshot",
         "msg": {"market_ticker": "K1", "yes_dollars_fp": [["0.40", "10.00"]],
                 "no_dollars_fp": []}},
        key_by_ticker, books)
    # YES side add: 10 -> 15 at $0.40 (fractional fixed-point size allowed).
    parse_kalshi_message(
        {"type": "orderbook_delta",
         "msg": {"market_ticker": "K1", "price_dollars": "0.40",
                 "delta_fp": "5.00", "side": "yes"}},
        key_by_ticker, books)
    assert books["kalshi:K1"].bids[0.40] == Decimal("15.00")
    # NO delta at $0.55 -> YES ask at $0.45.
    parse_kalshi_message(
        {"type": "orderbook_delta",
         "msg": {"market_ticker": "K1", "price_dollars": "0.55",
                 "delta_fp": "3.00", "side": "no"}},
        key_by_ticker, books)
    assert books["kalshi:K1"].asks[0.45] == Decimal("3.00")


def test_parse_kalshi_delta_missing_price_does_not_crash():
    # A partial/heartbeat delta with no price_dollars must be ignored, not raise
    # (the old int-cents parser did `100 - None` and killed the socket task).
    books = {}
    key_by_ticker = {"K1": "kalshi:K1"}
    parse_kalshi_message(
        {"type": "orderbook_delta",
         "msg": {"market_ticker": "K1", "side": "no", "delta_fp": "5.00"}},
        key_by_ticker, books)
    assert books["kalshi:K1"].to_order_book().best_ask is None


def test_parse_polymarket_us_full_snapshot():
    # Every .us frame is a full order-book snapshot keyed by marketSlug.
    books = {}
    key_by_slug = {"champ-a": "polymarket:champ-a"}
    parse_polymarket_us_message(
        {"subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
         "marketData": {"marketSlug": "champ-a",
                        "bids": [{"px": {"value": "0.14", "currency": "USD"}, "qty": "1478.0"}],
                        "offers": [{"px": {"value": "0.16", "currency": "USD"}, "qty": "212.0"}]}},
        key_by_slug, books)
    ob = books["polymarket:champ-a"].to_order_book()
    assert ob.best_bid.price == 0.14 and ob.best_bid.size == Decimal("1478.0")
    assert ob.best_ask.price == 0.16 and ob.best_ask.size == Decimal("212.0")


def test_parse_polymarket_us_heartbeat_ignored():
    books = {}
    parse_polymarket_us_message({"heartbeat": {}}, {"s": "k"}, books)
    assert books == {}


def test_parse_polymarket_book_and_price_change():
    # Real wire format: book snapshot carries asset_id + bids/asks of {price,size};
    # price_change carries `price_changes` with a per-change asset_id.
    books = {}
    key_by_token = {"tok1": "polymarket:tok1"}
    parse_polymarket_message(
        {"event_type": "book", "asset_id": "tok1",
         "bids": [{"price": "0.48", "size": "100"}],
         "asks": [{"price": "0.52", "size": "80"}]},
        key_by_token, books)
    parse_polymarket_message(
        {"event_type": "price_change", "market": "0xcond",
         "price_changes": [
             {"asset_id": "tok1", "side": "BUY", "price": "0.49", "size": "50",
              "best_bid": "0.49", "best_ask": "0.52"},
             {"asset_id": "OTHER", "side": "SELL", "price": "0.30", "size": "9"}]},
        key_by_token, books)
    ob = books["polymarket:tok1"].to_order_book()
    assert ob.best_bid.price == 0.49 and ob.best_bid.size == Decimal(50)
    assert ob.best_ask.price == 0.52
    # The change for an unsubscribed asset_id ("OTHER") is ignored.
    assert "OTHER" not in {b.market_key for b in books.values()}


@pytest.mark.asyncio
async def test_engine_reads_from_stream_without_rest():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    # REST source returns a DIFFERENT (worse) book; the stream must win.
    rest = FakeExchange(Venue.KALSHI, [m], {m.key: book(m, bid=0.10, bid_sz=1, ask=0.90, ask_sz=1)})
    router = ExchangeRouter({Venue.KALSHI: rest})

    mgr = StreamManager(clients=[])           # no live clients in the test
    mgr.active = True
    lb = LocalBook(m.key)
    lb.set_snapshot(bids=[(0.40, 50)], asks=[(0.42, 50)])
    mgr.books[m.key] = lb

    s = Settings(live=False, paper_starting_cash=Decimal(1000),
                 streaming=StreamingSettings(enabled=True))
    engine = Engine(s, router, [build("arbitrage")], stream=mgr)
    engine.markets = [m]

    books = await engine._refresh_books()
    assert books[m.key].best_bid.price == 0.40   # from the stream, not the 0.10 REST book
