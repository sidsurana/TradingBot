"""Polymarket adapter: UMA metadata stamping + resolution fetch."""

from __future__ import annotations

import json

import httpx
import pytest

from tradingbot.config import PolymarketCreds
from tradingbot.exchanges.polymarket import (
    PolymarketExchange,
    parse_gamma_markets,
    parse_gamma_resolutions,
)
from tradingbot.models import Market, Venue


def _gamma_market(cond: str, ids, outcomes, *, uma="", prices=None, **extra) -> dict:
    m = {
        "conditionId": cond,
        "question": f"Q {cond}",
        "clobTokenIds": json.dumps(ids),
        "outcomes": json.dumps(outcomes),
        "umaResolutionStatus": uma,
    }
    if prices is not None:
        m["outcomePrices"] = json.dumps(prices)
    m.update(extra)
    return m


# --- 1) UMA metadata ---------------------------------------------------------

def test_parse_gamma_stamps_uma_status():
    data = [_gamma_market("c1", ["t1", "t2"], ["Yes", "No"], uma="resolved")]
    markets = parse_gamma_markets(data, Venue.POLYMARKET)
    assert markets
    for m in markets:
        assert "uma_status" in m.metadata
        # existing keys preserved
        assert {"volume", "category", "is_sports", "end_ts"} <= set(m.metadata)
        assert m.metadata["uma_status"] == "resolved"


def test_parse_gamma_uma_status_disputed_and_default():
    disputed = parse_gamma_markets(
        [_gamma_market("c2", ["t3", "t4"], ["Yes", "No"], uma="DISPUTED")],
        Venue.POLYMARKET,
    )
    assert all(m.metadata["uma_status"] == "disputed" for m in disputed)

    # Absent umaResolutionStatus -> "" (never missing).
    m = {"conditionId": "c3", "question": "q",
         "clobTokenIds": json.dumps(["t5", "t6"]),
         "outcomes": json.dumps(["Yes", "No"])}
    absent = parse_gamma_markets([m], Venue.POLYMARKET)
    assert all(x.metadata["uma_status"] == "" for x in absent)


# --- 2) resolution parsing (pure) --------------------------------------------

def test_parse_gamma_resolutions_winner_loser_and_omissions():
    data = [
        _gamma_market("c1", ["yes1", "no1"], ["Yes", "No"],
                      uma="resolved", prices=["1", "0"]),          # YES won
        _gamma_market("c2", ["yes2", "no2"], ["Yes", "No"],
                      uma="resolved", prices=["0", "1"]),          # NO won
        _gamma_market("c3", ["yes3", "no3"], ["Yes", "No"],
                      uma="", prices=["0.4", "0.6"]),              # still open
        _gamma_market("c4", ["yes4", "no4"], ["Yes", "No"],
                      uma="disputed", prices=["1", "0"]),          # disputed
        _gamma_market("c5", ["yes5", "no5"], ["Yes", "No"],
                      uma="resolved"),                             # no outcomePrices
    ]
    held = {f"polymarket:{t}" for t in
            ("yes1", "no1", "yes2", "no2", "yes3", "yes4", "yes5")}
    res = parse_gamma_resolutions(data, held)
    assert res == {
        "polymarket:yes1": 1.0,   # winner
        "polymarket:no1": 0.0,    # loser
        "polymarket:yes2": 0.0,   # loser
        "polymarket:no2": 1.0,    # winner
        # yes3 open, yes4 disputed, yes5 no prices -> omitted entirely
    }


def test_parse_gamma_resolutions_only_returns_held_tokens():
    data = [_gamma_market("c1", ["yes1", "no1"], ["Yes", "No"],
                          uma="resolved", prices=["1", "0"])]
    res = parse_gamma_resolutions(data, {"polymarket:yes1"})
    assert res == {"polymarket:yes1": 1.0}  # no1 not held -> absent


def test_parse_gamma_resolutions_void_both_outcomes_normalize_to_half():
    """A VOID/refund reports every outcome at ~1.0. Redemption must NOT pay $1 on
    both legs of a mutually-exclusive set (that fabricates a $2 payout on a <$1
    dutch book); it normalizes to $0.50 each so the set redeems exactly $1.

    Seeded with the two real void conditionIds from the arb book that inflated
    the paper P&L by ~$105: Dota "Vici Gaming vs PlayTime" and tennis
    "Onclin vs McCabe", both of which resolved with outcomePrices ["1", "1"]."""
    data = [
        _gamma_market("0x22ff62a08fc3b84a", ["vici", "playtime"],
                      ["Vici Gaming", "PlayTime"], uma="resolved", prices=["1", "1"]),
        _gamma_market("0xfe088b02cc9f951a", ["onclin", "mccabe"],
                      ["Gauthier Onclin", "James McCabe"], uma="resolved",
                      prices=["1", "1"]),
    ]
    held = {f"polymarket:{t}" for t in ("vici", "playtime", "onclin", "mccabe")}
    res = parse_gamma_resolutions(data, held)
    assert res == {
        "polymarket:vici": 0.5, "polymarket:playtime": 0.5,
        "polymarket:onclin": 0.5, "polymarket:mccabe": 0.5,
    }
    # Invariant: each conditionId's redemptions sum to exactly $1.
    assert res["polymarket:vici"] + res["polymarket:playtime"] == 1.0
    assert res["polymarket:onclin"] + res["polymarket:mccabe"] == 1.0


def test_parse_gamma_resolutions_all_zero_left_unsettled():
    """An all-zero outcome set carries no redemption signal (total == 0); the
    market is omitted rather than dividing by zero or booking phantom values."""
    data = [_gamma_market("c9", ["a", "b"], ["Yes", "No"],
                          uma="resolved", prices=["0", "0"])]
    res = parse_gamma_resolutions(data, {"polymarket:a", "polymarket:b"})
    assert res == {}


# --- 2b) fetch_resolutions over a mocked Gamma transport ---------------------

def _held(token: str, cond: str) -> Market:
    return Market(venue=Venue.POLYMARKET, market_id=token, event_id=cond,
                  title=cond, outcome="Yes")


@pytest.mark.asyncio
async def test_fetch_resolutions_via_mock_transport():
    payload = [
        _gamma_market("c1", ["yes1", "no1"], ["Yes", "No"],
                      uma="resolved", prices=["1", "0"]),
        _gamma_market("c2", ["yes2", "no2"], ["Yes", "No"],
                      uma="resolved", prices=["0", "1"]),
        _gamma_market("c3", ["yes3", "no3"], ["Yes", "No"],
                      uma="", prices=["0.5", "0.5"]),
        _gamma_market("c4", ["yes4", "no4"], ["Yes", "No"],
                      uma="disputed", prices=["1", "0"]),
    ]
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=payload)

    ex = PolymarketExchange(PolymarketCreds())
    ex._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    held = [_held("yes1", "c1"), _held("no2", "c2"),
            _held("yes3", "c3"), _held("yes4", "c4")]
    res = await ex.fetch_resolutions(held)
    await ex._client.aclose()

    assert res == {"polymarket:yes1": 1.0, "polymarket:no2": 1.0}
    # queried by condition, including closed markets
    assert seen["params"].get("closed") == "true"
    assert "c1" in seen["params"].get_list("condition_ids")


@pytest.mark.asyncio
async def test_fetch_resolutions_swallows_network_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    ex = PolymarketExchange(PolymarketCreds())
    ex._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = await ex.fetch_resolutions([_held("yes1", "c1")])
    await ex._client.aclose()
    assert res == {}  # never raises; partial/empty on failure


@pytest.mark.asyncio
async def test_fetch_resolutions_empty_inputs():
    ex = PolymarketExchange(PolymarketCreds())
    assert await ex.fetch_resolutions([]) == {}  # no client needed, no markets
