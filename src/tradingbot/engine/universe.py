"""Universe curation — which markets the bot tracks.

Discovery returns a wide list of markets; this turns it into a *curated*
universe: drop untradeable/illiquid markets, optionally constrain by category or
a watchlist, rank by 24h volume, and keep the top-N per venue. Strategies then
pick within this universe by edge/spread, and the risk manager bounds exposure.

Pure and fully unit-tested — it operates on already-fetched Market objects whose
metadata carries `volume` and `category` (populated by the venue adapters).
"""

from __future__ import annotations

from collections import defaultdict

import structlog

from tradingbot.config import UniverseSettings
from tradingbot.models import Market

log = structlog.get_logger(__name__)


class UniverseSelector:
    def __init__(self, settings: UniverseSettings):
        self.settings = settings

    def select(self, markets: list[Market]) -> list[Market]:
        s = self.settings
        watchlist = set(s.watchlist)
        categories = {c.lower() for c in s.categories}

        filtered: list[Market] = []
        for m in markets:
            if watchlist and not (m.market_id in watchlist or m.event_id in watchlist):
                continue
            if categories and m.metadata.get("category", "") not in categories:
                continue
            if self._volume(m) < s.min_volume:
                continue
            filtered.append(m)

        # Rank by volume within each venue and keep the most liquid N. Ranking
        # per-venue avoids comparing USDC volume (Polymarket) to contract counts
        # (Kalshi) on the same scale.
        by_venue: dict = defaultdict(list)
        for m in filtered:
            by_venue[m.venue].append(m)

        out: list[Market] = []
        for venue, group in by_venue.items():
            group.sort(key=self._volume, reverse=True)
            out += group[: s.max_per_venue]
        return out

    @staticmethod
    def _volume(m: Market) -> float:
        try:
            return float(m.metadata.get("volume", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
