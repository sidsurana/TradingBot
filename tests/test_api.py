from decimal import Decimal

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradingbot.ai import BotController  # noqa: E402
from tradingbot.api import build_app  # noqa: E402
from tradingbot.config import Settings  # noqa: E402
from tradingbot.engine import Engine, ExchangeRouter  # noqa: E402
from tradingbot.models import Venue  # noqa: E402
from tradingbot.strategies import build  # noqa: E402
from tests.fake_exchange import FakeExchange, book, market  # noqa: E402

TOKEN = "secret-token"


def _client() -> TestClient:
    m = market(Venue.KALSHI, "K1", "E", "YES")
    kx = FakeExchange(Venue.KALSHI, [m], {m.key: book(m, bid=0.4, bid_sz=10, ask=0.42, ask_sz=10)})
    router = ExchangeRouter({Venue.KALSHI: kx})
    engine = Engine(Settings(live=False, paper_starting_cash=Decimal(1000)), router, [build("arbitrage")])
    return TestClient(build_app(BotController(engine), TOKEN))


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_health_is_open():
    assert _client().get("/health").json() == {"ok": True}


def test_auth_required():
    c = _client()
    assert c.get("/portfolio").status_code == 401
    assert c.get("/portfolio", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/portfolio", headers=_auth()).status_code == 200


def test_reads():
    c = _client()
    assert c.get("/portfolio", headers=_auth()).json()["mode"] == "paper"
    assert c.get("/risk", headers=_auth()).json()["kill_switch"] is False
    skills_list = c.get("/skills", headers=_auth()).json()
    assert any(s["name"] == "regime_detection" for s in skills_list)


def test_skill_prompt_renders():
    c = _client()
    r = c.get("/skill_prompt/regime_detection", headers=_auth())
    assert r.status_code == 200
    assert "regime" in r.json()["prompt"].lower()
    assert c.get("/skill_prompt/bogus", headers=_auth()).status_code == 404


def test_sensitive_action_confirm_flow():
    c = _client()
    staged = c.post("/actions/deploy_capital", json={"amount": 100}, headers=_auth()).json()
    assert staged["needs_confirmation"] is True
    done = c.post("/confirm", json={"token": staged["token"]}, headers=_auth()).json()
    assert done["ok"] is True
