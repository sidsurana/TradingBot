"""Polymarket trading fees — the exact protocol model.

Source: https://docs.polymarket.com/trading/fees

  fee = shares * feeRate * p * (1 - p)

Two properties that matter for strategy design:
  - MAKERS pay zero. Only takers (marketable orders that cross) pay a fee.
  - The p*(1-p) shape peaks at p=0.50 and vanishes toward the extremes, so a
    dutch book built from EXTREME-priced legs (e.g. 0.97 + 0.02) is nearly
    fee-free, while a mid-priced one (0.45 + 0.45) is not. A flat per-notional
    fee gets both the shape and the magnitude wrong.

We model TAKER fees, because the strategies here fire marketable orders. feeRate
is category-dependent; an unknown/missing category defaults to the HIGHEST rate
so a risk-free arbitrage never fires on an underestimated fee (missing a real
arb is safe; taking a fake one loses money).
"""

from __future__ import annotations

from decimal import Decimal

# Taker fee rate by market category (docs.polymarket.com/trading/fees).
TAKER_FEE_RATES: dict[str, float] = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04, "politics": 0.04, "mentions": 0.04, "tech": 0.04,
    "economics": 0.05, "culture": 0.05, "weather": 0.05, "other": 0.05,
    "geopolitics": 0.0,
}
# Unknown category -> highest rate (conservative for arbitrage: overestimate fees).
DEFAULT_TAKER_RATE = 0.07


def taker_fee_rate(category: str | None) -> float:
    return TAKER_FEE_RATES.get((category or "").lower().strip(), DEFAULT_TAKER_RATE)


def taker_fee_per_share(price: float, category: str | None) -> float:
    """Per-share taker fee in USDC — size-independent, for cost thresholds.
    Zero for makers is not modeled here (this is the taker path); zero at the
    price extremes and for fee-free categories falls out of the formula."""
    rate = taker_fee_rate(category)
    if rate <= 0.0 or price <= 0.0 or price >= 1.0:
        return 0.0
    return rate * price * (1.0 - price)


# Kalshi taker fee — standard series: fee = 0.07 * price * (1 - price) per
# contract (kalshi.com/fee-schedule). Some series are cheaper or fee-free; 0.07
# is the standard/highest rate, so using it is conservative for arbitrage
# (never fire on an underestimated fee). Makers pay less; the arb path takes.
KALSHI_TAKER_RATE = 0.07


def kalshi_taker_fee_per_share(price: float) -> float:
    """Per-contract Kalshi taker fee in USD, size-independent (for cost
    thresholds). Standard rate 0.07 * p * (1-p); zero at the price extremes."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return KALSHI_TAKER_RATE * price * (1.0 - price)


def polymarket_taker_fee(price: float, size: Decimal, category: str | None) -> Decimal:
    """Taker fee in USDC for filling `size` shares at `price`."""
    per_share = taker_fee_per_share(price, category)
    if per_share <= 0.0:
        return Decimal(0)
    return Decimal(str(per_share)) * size
