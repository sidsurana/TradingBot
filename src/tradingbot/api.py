"""HTTP control API — the LLM-free surface of the bot.

Exposes the BotController's reads and gated actions, plus rendered Quant-skill
prompts, behind a shared bearer token. An external agent (e.g. the LangGraph
agent in `langgraph_agent/`) calls these endpoints as tools. The bot makes NO
LLM calls itself, so running it 24/7 costs nothing in model tokens — you only
pay when you invoke the agent.

FastAPI/uvicorn are optional (the `serve` extra); imported lazily so the core
package runs without them.
"""

from __future__ import annotations

import json

import structlog
from pydantic import BaseModel

from tradingbot.ai import skills
from tradingbot.ai.controller import BotController
from tradingbot.config import ApiSettings

log = structlog.get_logger(__name__)


class LimitBody(BaseModel):
    name: str
    value: float


class AmountBody(BaseModel):
    amount: float


class TokenBody(BaseModel):
    token: str


class OrderBody(BaseModel):
    venue: str
    market_id: str
    side: str
    size: float
    price: float


class SignalBody(BaseModel):
    venue: str
    market_id: str
    fair_value: float
    confidence: float


def _skill_context(controller: BotController, capital: str | None, risk: str | None) -> dict:
    ctx = {
        "portfolio": json.dumps(controller.portfolio_summary(), default=str),
        "positions": json.dumps(controller.list_positions(), default=str),
        "risk_limits": json.dumps(controller.risk_status()["limits"], default=str),
        "market_snapshot": json.dumps(controller.market_snapshot(), default=str),
    }
    if capital:
        ctx["capital"] = capital
    if risk:
        ctx["risk_per_trade"] = risk
    return ctx


def build_app(controller: BotController, token: str):
    from fastapi import Depends, FastAPI, Header, HTTPException

    app = FastAPI(title="TradingBot Control API", version="0.1.0")

    def auth(authorization: str = Header(default="")) -> None:
        supplied = authorization.removeprefix("Bearer ").strip()
        if not token or supplied != token:
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    Protected = [Depends(auth)]

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/portfolio", dependencies=Protected)
    def portfolio() -> dict:
        return controller.portfolio_summary()

    @app.get("/positions", dependencies=Protected)
    def positions() -> list:
        return controller.list_positions()

    @app.get("/risk", dependencies=Protected)
    def risk() -> dict:
        return controller.risk_status()

    @app.get("/goals", dependencies=Protected)
    def goals() -> dict:
        return controller.goal_progress()

    @app.get("/signals", dependencies=Protected)
    def signals() -> list:
        return controller.list_signals()

    @app.post("/signals", dependencies=Protected)
    def set_signal(body: SignalBody) -> dict:
        return controller.set_signal(body.venue, body.market_id, body.fair_value,
                                     body.confidence)

    @app.get("/markets", dependencies=Protected)
    def markets(limit: int = 25) -> list:
        return controller.market_snapshot(limit)

    @app.get("/skills", dependencies=Protected)
    def list_skills() -> list:
        return [{"name": s.name, "description": s.description} for s in skills.available_skills()]

    @app.get("/skill_prompt/{name}", dependencies=Protected)
    def skill_prompt(name: str, capital: str | None = None, risk_per_trade: str | None = None):
        skill = skills.get_skill(name)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"unknown skill {name}")
        return {"name": name, "prompt": skill.render(_skill_context(controller, capital, risk_per_trade))}

    @app.post("/pause", dependencies=Protected)
    def pause() -> dict:
        return controller.pause()

    @app.post("/resume", dependencies=Protected)
    def resume() -> dict:
        return controller.resume()

    @app.post("/actions/set_risk_limit", dependencies=Protected)
    def set_risk_limit(body: LimitBody) -> dict:
        return controller.request_set_risk_limit(body.name, body.value)

    @app.post("/actions/deploy_capital", dependencies=Protected)
    def deploy_capital(body: AmountBody) -> dict:
        return controller.request_deploy_capital(body.amount)

    @app.post("/actions/place_order", dependencies=Protected)
    def place_order(body: OrderBody) -> dict:
        return controller.request_place_order(body.venue, body.market_id, body.side,
                                              body.size, body.price)

    @app.post("/actions/go_live", dependencies=Protected)
    def go_live() -> dict:
        return controller.request_go_live()

    @app.post("/actions/kill_switch", dependencies=Protected)
    def kill_switch() -> dict:
        return controller.request_trip_kill_switch()

    @app.post("/confirm", dependencies=Protected)
    def confirm(body: TokenBody) -> dict:
        return controller.confirm(body.token)

    @app.post("/cancel", dependencies=Protected)
    def cancel(body: TokenBody) -> dict:
        return controller.cancel(body.token)

    return app


class ApiServer:
    """Runs the control API inside the bot's asyncio loop (uvicorn)."""

    def __init__(self, settings: ApiSettings, controller: BotController):
        self.settings = settings
        self.controller = controller
        self._server = None

    async def run(self) -> None:
        if not self.settings.enabled:
            log.info("api.disabled")
            return
        if not self.settings.token:
            log.error("api.no_token", msg="set TB_API_TOKEN to enable the control API")
            return
        import uvicorn

        app = build_app(self.controller, self.settings.token)
        config = uvicorn.Config(app, host=self.settings.host, port=self.settings.port,
                                log_level="warning")
        self._server = uvicorn.Server(config)
        log.info("api.started", host=self.settings.host, port=self.settings.port)
        await self._server.serve()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
