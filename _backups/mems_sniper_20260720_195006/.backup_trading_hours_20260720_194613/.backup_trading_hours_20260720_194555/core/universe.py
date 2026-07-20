"""Universe builder - which symbols we watch and trade.

Two modes:
  1. Futures-only (signals): Top N coins by volume that have USDT-M perpetual
     futures — major coins like BTC, ETH, SOL, DOGE, etc.
  2. Normal (scalping/meme): meme keywords + new listings + top volume
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Set

from core.exchange import ExchangeManager, SymbolInfo
from core.logging_setup import logger


class UniverseBuilder:
    def __init__(self, em: ExchangeManager, settings) -> None:
        self.em = em
        self.s = settings
        self.cfg = self.s.universe

    async def build(self, exchange: str, futures_only: bool = False) -> List[SymbolInfo]:
        """Return the active watchlist for an exchange.
        
        If futures_only=True: only top-volume coins that have USDT-M perpetuals.
        These are established, well-known coins traded on Binance/Bybit futures.
        """
        if futures_only:
            return await self._build_futures_universe(exchange)
        return await self._build_spot_universe(exchange)

    async def _build_futures_universe(self, exchange: str) -> List[SymbolInfo]:
        """Top coins by volume that have USDT-M perpetual futures.
        
        These are major, well-known coins: BTC, ETH, SOL, BNB, DOGE, XRP, etc.
        No obscure tokens — only established assets with deep liquidity.
        Works with both spot and futures defaultType.
        """
        try:
            client = self.em.clients[exchange]
            
            # Load markets to ensure we have them
            if not client.markets:
                await client.load_markets()
            
            # Get all USDT-M perpetual swap symbols
            futures_syms = []
            for mk_sym, mk in client.markets.items():
                if mk.get("swap") and mk.get("linear") and mk.get("quote") == "USDT":
                    base = mk.get("base", "")
                    spot_sym = f"{base}/USDT"
                    futures_syms.append((spot_sym, base, mk_sym))
            
            # Fetch tickers for volume data
            tickers = await client.fetch_tickers()
            
            candidates = []
            for spot_sym, base, mk_sym in futures_syms:
                # Try to get volume from swap ticker first, then spot
                vol = 0
                if mk_sym in tickers:
                    vol = tickers[mk_sym].get("quoteVolume", 0) or 0
                elif spot_sym in tickers:
                    vol = tickers[spot_sym].get("quoteVolume", 0) or 0
                if vol <= 0:
                    continue
                candidates.append((spot_sym, base, vol))
            
            # Sort by volume descending — top coins are the most established
            candidates.sort(key=lambda x: x[2], reverse=True)
            
            # Top N futures coins by volume
            max_futures = int(self.cfg.get("futures_universe_size", 200))
            result = []
            for sym, base, vol in candidates[:max_futures]:
                result.append(SymbolInfo(
                    symbol=sym, base=base, quote="USDT",
                    listed_at=None, has_futures=True,
                ))
            
            logger.info(
                f"Universe[{exchange}] FUTURES: {len(result)} symbols "
                f"(top {max_futures} by volume with perpetual futures)"
            )
            return result
            
        except Exception as exc:
            logger.error(f"Universe futures build failed for {exchange}: {exc}")
            return []

    async def _build_spot_universe(self, exchange: str) -> List[SymbolInfo]:
        """Normal universe: meme keywords + new listings + top volume."""
        all_syms: List[SymbolInfo] = []
        try:
            all_syms = await self.em.list_usdt_spot_symbols(exchange)
        except Exception as exc:
            logger.error(f"Universe: failed to list symbols for {exchange}: {exc}")
            return []

        keywords: Set[str] = set(map(str.upper, self.cfg.get("meme_keywords", [])))
        new_hours = float(self.cfg.get("include_new_listed_hours", 72))
        min_vol = float(self.cfg.get("min_quote_volume_24h", 1_000_000))
        max_vol = float(self.cfg.get("max_quote_volume_24h", 500_000_000))

        info_map = {s.symbol: s for s in all_syms}

        vol_map = {}
        try:
            tickers = await self.em.clients[exchange].fetch_tickers()
            for sym, tkr in tickers.items():
                if sym in info_map:
                    vol_map[sym] = float(tkr.get("quoteVolume") or 0)
        except Exception as exc:
            logger.warning(f"Universe: ticker fetch failed for {exchange}: {exc}")

        chosen: List[SymbolInfo] = []
        seen: Set[str] = set()

        for s in all_syms:
            if s.base and s.base.upper() in keywords:
                chosen.append(s)
                seen.add(s.symbol)

        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(new_hours * 3600 * 1000)
        for s in all_syms:
            if s.symbol in seen:
                continue
            if s.listed_at and s.listed_at >= cutoff:
                chosen.append(s)
                seen.add(s.symbol)

        top_n = 60
        try:
            top = await self.em.fetch_top_volume_symbols(exchange, limit=top_n)
        except Exception as exc:
            logger.warning(f"Universe: fallback fetch failed: {exc}")
            top = []
        for s in top:
            if s.symbol in seen:
                continue
            chosen.append(s)
            seen.add(s.symbol)

        filtered: List[SymbolInfo] = []
        for s in chosen:
            qv = vol_map.get(s.symbol, 0)
            if qv == 0 and s.symbol not in vol_map:
                filtered.append(s)
                continue
            if qv < min_vol:
                continue
            if qv > max_vol:
                if s.listed_at and s.listed_at >= cutoff:
                    filtered.append(s)
                continue
            filtered.append(s)

        logger.info(
            f"Universe[{exchange}]: {len(filtered)} symbols "
            f"(candidates before filter: {len(chosen)})"
        )
        return filtered


async def build_universes(em: ExchangeManager, settings, futures_only: bool = False) -> dict:
    """Build universe for every enabled exchange. Returns {exchange: [SymbolInfo]}."""
    out = {}
    ub = UniverseBuilder(em, settings)
    for name, conf in settings.exchanges.items():
        if not conf.get("enable", True):
            continue
        out[name] = await ub.build(name, futures_only=futures_only)
    return out
