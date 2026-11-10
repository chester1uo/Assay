"""Market routing shared by the controller and distributed workers."""

from __future__ import annotations

import dataclasses
import os

UNIVERSE_MARKETS = {
    **dict.fromkeys(("NASDAQ100", "SP500", "RUSSELL2000", "TOP500", "TOP1000", "TOP3000"), "US"),
    **dict.fromkeys(("CSI300", "CSI500", "CSI1000", "CSI800"), "CN"),
    **dict.fromkeys(("HK100", "HSHARE100", "HSI", "HSTECH"), "HK"),
}


def market_for(universe):
    try:
        return UNIVERSE_MARKETS[universe.upper()]
    except KeyError:
        raise ValueError(f"unknown universe: {universe}") from None


class MarketStores:
    def __init__(self, stores):
        self.stores = stores

    @classmethod
    def from_env(cls, us=None):
        from assay.config import AssayConfig
        from assay.data.store.qfv_store import QFVPanelStore
        from assay.data.store.canonical import CanonicalPanelStore

        cfg = AssayConfig.from_env()
        stores = {"US": us or QFVPanelStore(os.environ["ASSAY_QFV_PANEL_DIR"])}
        for market in ("CN", "HK"):
            if market in cfg.market_dirs:
                stores[market] = CanonicalPanelStore(dataclasses.replace(
                    cfg, market=market, data_dir=cfg.market_dirs[market]))
        return cls(stores)

    @property
    def snapshot_id(self):
        return self.stores["US"].snapshot_id

    def refresh(self):
        for store in self.stores.values():
            if hasattr(store, "refresh"):
                store.refresh()

    def versions(self):
        self.refresh()
        return {m: s.snapshot_id for m, s in self.stores.items() if getattr(s, "snapshot_id", None)}

    def for_universe(self, universe):
        market = market_for(universe)
        store = self.stores.get(market)
        if store is None or not getattr(store, "snapshot_id", None):
            raise ValueError(f"{market} data snapshot is unavailable on this host")
        return store

    def latest(self, universe):
        store = self.for_universe(universe)
        return store.price_span()["last_date"]

    def end(self, universe, requested=None):
        latest = self.latest(universe)
        if requested is not None:
            from datetime import date

            if date.fromisoformat(requested) > date.fromisoformat(latest):
                raise ValueError(f"{universe} data available through {latest}; requested end {requested}")
        return requested or latest
