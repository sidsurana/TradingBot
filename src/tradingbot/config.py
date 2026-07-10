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
    # Correlation filter: groups of market keys (e.g. [["data:SPY","data:QQQ"]])
    # that may not hold same-direction exposure simultaneously. An order that
    # would OPEN or EXTEND a position in the same direction as an existing
    # nonzero position in another member of its group is rejected; reducing/
    # closing orders always pass.
    correlation_groups: list[list[str]] = Field(default_factory=list)


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
    emit_cooldown_s: float = 30.0            # min seconds between exit re-emits per market


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
    model: str = "claude-haiku-4-5-20251001"  # Haiku only — Opus costs too much for this use
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


class DataFeedSettings(BaseSettings):
    """The DATA venue: directional instruments (index ETFs, BTC, gold, oil)
    fed by free public endpoints (Yahoo v8 chart API for equities/commodities,
    Coinbase Exchange public REST for crypto). Read-only market data — paper
    execution simulates fills against a synthetic two-sided book around the
    last price."""

    model_config = SettingsConfigDict(env_prefix="TB_DATA_", env_file=".env", extra="ignore")

    enabled: bool = False
    only: bool = False               # wire ONLY the data venue (skip Kalshi/Polymarket)
    equities: list[str] = Field(default_factory=lambda: ["SPY", "QQQ"])
    crypto: list[str] = Field(default_factory=lambda: ["BTC-USD"])
    # Front-month futures trade ~23h/day => clean 4h bars (GLD/USO only bar
    # during equity hours). Swap to ["GLD","USO"] to mirror ETF execution.
    commodities: list[str] = Field(default_factory=lambda: ["GC=F", "CL=F"])
    quote_ttl_s: float = 20.0        # cache quotes/books this long (rate-limit shield)
    history_bars: int = 200          # candles to keep per (symbol, interval)
    synthetic_spread_bps: float = 2.0  # half-spread applied around last price
    request_timeout_s: float = 10.0


class MeanReversionSettings(BaseSettings):
    """15m mean reversion on the equity indices: fade stretched moves back to
    the rolling mean when z-score exceeds entry_z; close at exit_z."""

    model_config = SettingsConfigDict(env_prefix="TB_MR_", env_file=".env", extra="ignore")

    interval: str = "15m"
    lookback: int = 20               # bars for rolling mean/stddev
    entry_z: float = 2.0             # |z| to open a fade
    exit_z: float = 0.3              # |z| at which the snap-back is "done"
    rsi_period: int = 2              # fast RSI confirmation
    rsi_oversold: float = 10.0
    rsi_overbought: float = 90.0
    min_bars: int = 40               # don't trade before this much history


class BreakoutSettings(BaseSettings):
    """1h momentum breakout on BTC: enter when price closes through the
    Donchian channel extreme on volume >= volume_mult x average."""

    model_config = SettingsConfigDict(env_prefix="TB_BO_", env_file=".env", extra="ignore")

    interval: str = "1h"
    channel_period: int = 20         # Donchian lookback
    volume_period: int = 20
    volume_mult: float = 1.5         # breakout bar volume vs average
    min_bars: int = 40


class TrendSettings(BaseSettings):
    """4h trend following on gold/oil: EMA-cross direction with entries on
    the cross, exits on the opposite cross or the hard stop."""

    model_config = SettingsConfigDict(env_prefix="TB_TREND_", env_file=".env", extra="ignore")

    interval: str = "4h"
    fast_ema: int = 20
    slow_ema: int = 50
    min_bars: int = 120


class SizingSettings(BaseSettings):
    """Volatility-aware position sizing shared by the directional strategies.
    Risk a fixed fraction of equity per trade; per-unit risk is the larger of
    the hard stop distance and atr_mult x ATR, so positions shrink when the
    market is volatile."""

    model_config = SettingsConfigDict(env_prefix="TB_SIZING_", env_file=".env", extra="ignore")

    risk_per_trade_pct: float = 0.005    # fraction of equity risked per trade
    hard_stop_pct: float = 0.01          # the non-negotiable 1% stop (mirrors TB_EXIT_STOP_LOSS_PCT)
    atr_period: int = 14
    atr_mult: float = 1.5
    max_notional_per_trade: Decimal = Decimal(500)


class CertaintyCarrySettings(BaseSettings):
    """Certainty Carry — the Polymarket near-certain carry harvest.

    Buy outcome tokens priced in a narrow high band (structurally underpriced
    vs their true resolution probability, due to favorite-longshot bias and the
    zero-yield-collateral discount), hold to on-chain resolution for the $1
    redemption. Downside is managed by (a) entry filters that avoid gap-prone
    and informed-flow markets, (b) a complement-set exit (buy the OTHER outcome
    to lock a guaranteed $1 set when a position deteriorates — never a naive
    price stop, which whipsaws and can't fill through a resolution gap), and
    (c) position sizing that survives a full -100% loss.
    """

    model_config = SettingsConfigDict(env_prefix="TB_CARRY_", env_file=".env", extra="ignore")

    price_min: float = 0.94          # only acquire tokens whose best ask is in
    price_max: float = 0.96          # [price_min, price_max]
    max_spread: float = 0.015        # skip wide books (spread eats the edge + adverse selection)
    min_volume_24h: float = 5000.0   # require real liquidity ($ 24h)
    min_hours_to_resolution: float = 48.0   # NEVER buy closer than this — near-resolution
    max_days_to_resolution: float = 14.0    # tokens are disproportionately informed flow
    min_annualized_carry: float = 1.0       # require >=100%/yr annualized carry to bother
    max_positions: int = 8                  # concurrent position cap
    max_per_group: int = 2                  # cap per correlation group (category + subject)
    sleeve_fraction: float = 0.40           # <= this fraction of equity deployed at once
    max_notional_per_position: Decimal = Decimal(50)  # $ per market
    # Exit: if a held token's mid falls below this, take the better of
    # (sell at bid) vs (buy complement to lock a $1 set) — reduce-only.
    complement_exit_below: float = 0.85
    require_uma_ok: bool = True       # only enter markets whose uma_status is clean (not disputed)
    # Skip markets whose resolution is subjective / gaps on a single headline.
    veto_keywords: list[str] = Field(default_factory=lambda: [
        "announce", "confirm", "tweet", "say", "said", "claim"])
    # Only trade continuously-observable-underlying categories in v1 (crypto/index
    # thresholds, poll-tracked politics). Empty => allow all non-vetoed markets.
    allowed_categories: list[str] = Field(default_factory=list)


class ArbitrageSettings(BaseSettings):
    """Dutch-book arbitrage — the one edge here that needs NO probability model.

    Within a complete set of mutually-exclusive outcomes, the cheapest asks must
    cost >= $1 (one outcome always redeems to $1). When they cost strictly less
    than $1 after fees, buying one of every outcome locks a risk-free profit —
    edge by construction, verifiable, no view on 'true probability' required.

    Two guards make it honest rather than a trap: (1) fee-aware — the threshold
    is cost*(1+fee_rate) < 1, not a hand-wave; (2) leg-risk buffer — min_edge is
    the net profit required beyond fees, a cushion against the book moving
    between legs (true atomic fills aren't available on-venue; see the strategy
    docstring). Loss is already capped by full collateralization, so no stop."""

    model_config = SettingsConfigDict(env_prefix="TB_ARB_", env_file=".env", extra="ignore")

    min_edge: float = 0.01           # required NET profit per $1 set, after fees (leg-risk buffer)
    max_size: Decimal = Decimal(20)  # units per leg, capped further by min depth across legs
    # Fees are the exact Polymarket taker model (fees.py: feeRate(category)*p*(1-p)),
    # not a flat rate — no knob here, it's read per-leg from each market's category.


class SettlementSettings(BaseSettings):
    """Paper redemption of resolved prediction-market positions. Resolved
    markets leave the active universe, so held tokens would otherwise strand at
    their last mark forever. This polls for resolution and books a synthetic
    redemption fill at $1 (winning token) or $0 (loser). Also fixes the same
    latent gap in the existing dutch-book arbitrage strategy."""

    model_config = SettingsConfigDict(env_prefix="TB_SETTLE_", env_file=".env", extra="ignore")

    enabled: bool = False
    poll_min: float = 5.0            # how often to poll Gamma for held-market resolution


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
    datafeed: DataFeedSettings = Field(default_factory=DataFeedSettings)
    mean_reversion: MeanReversionSettings = Field(default_factory=MeanReversionSettings)
    breakout: BreakoutSettings = Field(default_factory=BreakoutSettings)
    trend: TrendSettings = Field(default_factory=TrendSettings)
    sizing: SizingSettings = Field(default_factory=SizingSettings)
    carry: CertaintyCarrySettings = Field(default_factory=CertaintyCarrySettings)
    arbitrage: ArbitrageSettings = Field(default_factory=ArbitrageSettings)
    settlement: SettlementSettings = Field(default_factory=SettlementSettings)
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
