"""Configuration loaded from environment / .env.

Keep secrets out of code. Live trading is gated behind `TB_LIVE=true` *and*
explicit per-venue credentials, so the default of running this project does
nothing dangerous.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RiskLimits(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TB_RISK_", env_file=".env", extra="ignore")

    max_position_per_market: Decimal = Decimal(100)   # max abs contracts in one market
    max_notional_per_market: Decimal = Decimal(50)    # max $ at risk in one market
    max_gross_notional: Decimal = Decimal(500)        # max $ across the book
    max_daily_loss: Decimal = Decimal(50)             # kill-switch threshold ($)
    max_orders_per_min: int = 60                      # crude rate guard


class ExitSettings(BaseSettings):
    """Per-position stop-loss / take-profit. Thresholds are fractions of the
    entry price (0 disables that side). E.g. stop_loss_pct=0.25 closes a position
    once it has lost 25% of its entry value; take_profit_pct=0.5 closes at +50%.

    Default OFF, because pure arbitrage legs are meant to be held to resolution.
    Turn these on for directional (signal/MM) exposure."""

    model_config = SettingsConfigDict(env_prefix="TB_EXIT_", env_file=".env", extra="ignore")

    enabled: bool = False
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    min_size_to_exit: Decimal = Decimal(1)   # ignore dust positions


class KalshiCreds(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TB_KALSHI_", env_file=".env", extra="ignore")

    api_key_id: str = ""
    private_key_path: str = ""       # Kalshi uses RSA-signed requests
    base_url: str = "https://api.elections.kalshi.com"

    @property
    def configured(self) -> bool:
        return bool(self.api_key_id and self.private_key_path)


class PolymarketCreds(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TB_POLY_", env_file=".env", extra="ignore")

    private_key: str = ""            # wallet key for signing CLOB orders
    funder_address: str = ""
    clob_url: str = "https://clob.polymarket.com"
    discovery_max: int = 300         # paginate Gamma to this many markets (TB_POLY_DISCOVERY_MAX)

    @property
    def configured(self) -> bool:
        return bool(self.private_key)


class AnthropicCreds(BaseSettings):
    """Credentials + model config for the Claude agent ("the brain")."""

    model_config = SettingsConfigDict(env_prefix="TB_ANTHROPIC_", env_file=".env", extra="ignore")

    api_key: str = ""                # falls back to ANTHROPIC_API_KEY if empty
    model: str = "claude-opus-4-8"   # latest Opus; adaptive thinking
    effort: str = "high"             # low | medium | high | xhigh | max
    max_tokens: int = 8000

    @property
    def configured(self) -> bool:
        import os

        return bool(self.api_key or os.environ.get("ANTHROPIC_API_KEY"))


class TelegramCreds(BaseSettings):
    """Telegram bot for chatting with the agent from your phone."""

    model_config = SettingsConfigDict(env_prefix="TB_TELEGRAM_", env_file=".env", extra="ignore")

    bot_token: str = ""
    # Only these chat IDs may command the bot. Empty => reject everyone (safe default).
    allowed_chat_ids: list[int] = Field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.allowed_chat_ids)


class GoalSettings(BaseSettings):
    """Profit targets. The bot tracks PnL against these and the agent reports on
    them; when a daily target is hit it can lock gains by throttling risk."""

    model_config = SettingsConfigDict(env_prefix="TB_GOAL_", env_file=".env", extra="ignore")

    daily_target: Decimal = Decimal(0)    # $ profit/day target (0 => disabled)
    weekly_target: Decimal = Decimal(0)   # $ profit/week target (0 => disabled)
    lock_gains: bool = True               # throttle risk once the daily target is met
    state_path: str = ".tradingbot/goals.json"  # persists day/week baselines


class AutopilotSettings(BaseSettings):
    """The autonomous optimization loop that periodically reassesses and reports."""

    model_config = SettingsConfigDict(env_prefix="TB_AUTOPILOT_", env_file=".env", extra="ignore")

    enabled: bool = False
    interval_min: float = 30.0            # how often to run a cycle
    briefing: bool = True                 # push an agent briefing to Telegram each cycle


class LinkSettings(BaseSettings):
    """Cross-venue event mapping. Cross-venue arbitrage needs to know that, say,
    a Kalshi market and a Polymarket token resolve to the SAME real outcome —
    their native ids never match, so the link must be declared. Explicit (not
    fuzzy) by design: a wrong link trades two unrelated markets as equivalent, so
    these are user-curated and trusted.

    Each link group: {"link_id": "...", "members": [{"venue": "...", "market_id": "..."}, ...]}.
    Provide inline via `links` or as a JSON file at `map_path`."""

    model_config = SettingsConfigDict(env_prefix="TB_LINK_", env_file=".env", extra="ignore")

    map_path: str = ""                                   # JSON file of link groups
    links: list[dict] = Field(default_factory=list)      # inline link groups


class UniverseSettings(BaseSettings):
    """Which markets the bot tracks ("universe curation"). Discovery filters to
    genuinely tradeable, liquid markets, ranks by volume, caps per venue, and
    re-curates periodically (markets open/close/resolve). Strategies then pick
    within this universe by edge/spread."""

    model_config = SettingsConfigDict(env_prefix="TB_UNIVERSE_", env_file=".env", extra="ignore")

    max_per_venue: int = 100         # keep the top-N most-liquid markets per venue
    min_volume: float = 0.0          # drop markets below this 24h volume (0 = keep all)
    refresh_interval_min: float = 10.0  # re-curate this often (0 disables)
    categories: list[str] = Field(default_factory=list)  # e.g. ["politics","sports"]; empty = all
    watchlist: list[str] = Field(default_factory=list)   # market_ids/tickers/event_ids; empty = all


class MarketMakerSettings(BaseSettings):
    """Market-maker params. Quote both sides when a market's spread clears
    min_spread; size each side by quote_size, bounded by max_inventory; quote the
    widest-spread max_markets only."""

    model_config = SettingsConfigDict(env_prefix="TB_MM_", env_file=".env", extra="ignore")

    min_spread: float = 0.02       # only make when ask-bid >= this (covers fees + edge)
    quote_size: Decimal = Decimal(10)
    max_inventory: Decimal = Decimal(40)
    max_markets: int = 15
    requote_tolerance: float = 0.01  # only cancel/replace a resting quote when the target
                                     # price moves more than this (avoids churn/rate-limit on
                                     # sub-tick book jitter). 0 = requote on any change.
    # Market selection guards — never make markets in event-driven contracts (live
    # sports, exact-score, anything resolving soon): their wide spreads are adverse-
    # selection traps, not edge. Informed traders (anyone watching the game) pick you off.
    exclude_sports: bool = True            # skip markets flagged sports / in-play
    min_hours_to_resolution: float = 24.0  # skip markets resolving within this many hours


class SignalSettings(BaseSettings):
    """Signal/model strategy — takes directional positions from fair-value views
    (pushed by the agent's alpha/regime skills), sized by fractional Kelly.
    Conservative by default; protect positions with the stop-loss exits."""

    model_config = SettingsConfigDict(env_prefix="TB_SIGNAL_", env_file=".env", extra="ignore")

    kelly_fraction: float = 0.25        # fraction of full Kelly (0.25 = quarter-Kelly)
    bankroll: Decimal = Decimal(500)    # capital allocated to this strategy ($)
    min_edge: float = 0.05              # require |fair_value - price| >= this to act
    min_confidence: float = 0.5         # ignore signals below this confidence
    max_signal_age_s: float = 3600.0    # ignore signals older than this (stale views)
    max_position: Decimal = Decimal(50)  # hard cap on contracts per market


class StreamingSettings(BaseSettings):
    """WebSocket streaming order books — push updates instead of REST polling, so
    data is sub-second fresh and you avoid per-market rate limits. Polymarket's
    market channel is public; Kalshi's requires the same API credentials as
    trading (so Kalshi streaming only runs when Kalshi is configured)."""

    model_config = SettingsConfigDict(env_prefix="TB_STREAMING_", env_file=".env", extra="ignore")

    enabled: bool = False
    rest_fallback_cap: int = 20   # max markets to REST-fill per tick during WS warm-up
    # When streaming, book reads are ~free (in-memory), so the engine can act far
    # more often than the REST cadence. This is the act-on-edge latency ceiling.
    loop_interval_s: float = 0.25
    # Event-driven acting: react the instant a book updates, instead of waiting for
    # the loop. The loop stays on as the backstop (risk, exits, full reconcile,
    # heartbeat). react_debounce_s coalesces bursts of updates into one evaluation.
    event_driven: bool = True
    react_debounce_s: float = 0.01


class PersistenceSettings(BaseSettings):
    """Durable state so a restart doesn't lose positions or re-baseline goals.
    Event-sourced: every fill is persisted, then replayed on startup to rebuild
    the portfolio exactly. Needed before any unattended real-money run."""

    model_config = SettingsConfigDict(env_prefix="TB_PERSIST_", env_file=".env", extra="ignore")

    enabled: bool = False
    path: str = ".tradingbot/state.db"


class ApiSettings(BaseSettings):
    """HTTP control API — the surface an external (LangGraph) agent drives. The
    bot itself makes no LLM calls; this exposes reads, gated actions, and
    rendered quant-skill prompts behind a shared bearer token."""

    # env_file here too: nested settings don't inherit the parent's .env, so
    # without this TB_API_* in .env would be silently ignored (the agent could
    # never reach the control API). Real env vars still override the file.
    model_config = SettingsConfigDict(env_prefix="TB_API_", env_file=".env", extra="ignore")

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    token: str = ""   # required when enabled; the agent sends it as a bearer token


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TB_", env_file=".env", extra="ignore")

    live: bool = False               # MASTER SWITCH. False => paper trading only.
    loop_interval_s: float = 2.0     # engine tick cadence
    log_level: str = "INFO"
    paper_starting_cash: Decimal = Decimal(1000)

    enabled_strategies: list[str] = Field(default_factory=lambda: ["arbitrage"])

    universe: UniverseSettings = Field(default_factory=UniverseSettings)
    links: LinkSettings = Field(default_factory=LinkSettings)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    exits: ExitSettings = Field(default_factory=ExitSettings)
    market_maker: MarketMakerSettings = Field(default_factory=MarketMakerSettings)
    signal: SignalSettings = Field(default_factory=SignalSettings)
    kalshi: KalshiCreds = Field(default_factory=KalshiCreds)
    polymarket: PolymarketCreds = Field(default_factory=PolymarketCreds)
    anthropic: AnthropicCreds = Field(default_factory=AnthropicCreds)
    telegram: TelegramCreds = Field(default_factory=TelegramCreds)
    goals: GoalSettings = Field(default_factory=GoalSettings)
    autopilot: AutopilotSettings = Field(default_factory=AutopilotSettings)
    persistence: PersistenceSettings = Field(default_factory=PersistenceSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    streaming: StreamingSettings = Field(default_factory=StreamingSettings)


def load_settings() -> Settings:
    return Settings()
