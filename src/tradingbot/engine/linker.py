"""Cross-venue event mapping.

Cross-venue arbitrage compares the same real outcome on two venues — but the
venues' native ids never match (Kalshi tickers vs Polymarket condition/token
ids), so without a mapping the same event never groups and cross-venue arb can't
fire. `EventLinker` applies a **user-curated** mapping: each link group lists the
(venue, market_id) members that resolve identically, sharing a `link_id`. The
linker stamps that `link_id` into each market's metadata; the arbitrage strategy
then groups by `link_id` so linked markets are treated as the same outcome.

Explicit, not fuzzy: a wrong link would trade two unrelated markets as if
equivalent, so links are declared and trusted (auto-suggestion is a separate,
advisory-only future feature).
"""

from __future__ import annotations

import json

import structlog

from tradingbot.config import LinkSettings
from tradingbot.models import Market

log = structlog.get_logger(__name__)


class EventLinker:
    def __init__(self, settings: LinkSettings):
        self._map: dict[tuple[str, str], str] = {}  # (venue, market_id) -> link_id
        groups = list(settings.links)
        if settings.map_path:
            try:
                with open(settings.map_path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    groups += loaded
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                log.warning("linker.map_load_failed", path=settings.map_path, error=str(exc))
        for g in groups:
            link_id = g.get("link_id")
            if not link_id:
                continue
            for member in g.get("members", []):
                venue, market_id = member.get("venue"), member.get("market_id")
                if venue and market_id:
                    self._map[(venue, market_id)] = link_id
        if self._map:
            log.info("linker.loaded", members=len(self._map),
                     links=len(set(self._map.values())))

    @property
    def count(self) -> int:
        return len(set(self._map.values()))

    def annotate(self, markets: list[Market]) -> None:
        """Stamp link_id into the metadata of any market that's in a link group."""
        for m in markets:
            link_id = self._map.get((m.venue.value, m.market_id))
            if link_id:
                m.metadata["link_id"] = link_id
