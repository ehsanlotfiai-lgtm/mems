"""DEX (decentralized exchange) connector.

Meme coins appear on DEXes FIRST — often hours or days before they
reach centralized exchanges. This module connects to the platforms
where the freshest meme coins launch:

  * Pump.fun  — Solana launchpad, ground-zero for new meme coins
  * Raydium    — main Solana DEX
  * Jupiter    — Solana DEX aggregator (best-price routing)
  * PancakeSwap — BNB Chain DEX for cheaper meme coins
  * Uniswap    — Ethereum DEX, where tokens go before CEX listing

Data sources (all free, no API key needed):
  * DexScreener API  — aggregates pairs across ALL of the above
  * Pump.fun API     — newest token launches on Solana
  * GeckoTerminal API — backup / cross-check

The module produces normalized `DEXToken` objects that feed into the
same strategy engine and universe builder as CEX symbols.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import httpx

from core.logging_setup import logger
from core.models import Candle
from core.exchange import SymbolInfo


# ==========================================================
# Adapter: DEXToken -> SymbolInfo (so strategy engine works on DEX)
# ==========================================================

def dex_token_to_symbol_info(t: DEXToken) -> SymbolInfo:
    """Wrap a DEXToken so the CEX-oriented ConfluenceEngine can consume it.
    The `symbol` field is set to a synthetic identifier encoding chain+address
    so the engine can route data requests back to the DEX manager."""
    return SymbolInfo(
        symbol=f"DEX:{t.chain}:{t.address}",
        base=t.symbol,
        quote="USDC" if t.chain == "solana" else "WETH",
        listed_at=int(t.created_at * 1000) if t.created_at else None,
    )


# ==========================================================
# Models
# ==========================================================

@dataclass
class DEXToken:
    """A meme coin trading on a DEX, normalized across platforms."""
    address: str                     # on-chain token contract address
    chain: str                       # solana | bsc | ethereum | base
    dex: str                         # pumpfun | raydium | pancakeswap | uniswap | jupiter
    symbol: str                      # token ticker (e.g. "BONK")
    name: str                        # full name
    pair_address: str = ""           # liquidity pool address
    price_usd: float = 0.0
    price_native: float = 0.0        # price in native token (SOL/BNB/ETH)
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    volume_6h_usd: float = 0.0
    volume_1h_usd: float = 0.0
    txns_24h: int = 0
    mcap: float = 0.0
    fdv: float = 0.0
    price_change_24h_pct: float = 0.0
    price_change_1h_pct: float = 0.0
    price_change_5m_pct: float = 0.0
    created_at: float = 0.0          # unix seconds — when pool was created
    age_seconds: float = 0.0
    socials: List[Dict[str, str]] = field(default_factory=list)
    url: str = ""

    @property
    def is_fresh(self) -> bool:
        """Token launched within the last hour — highest-alpha window."""
        return self.age_seconds < 3600

    @property
    def is_brand_new(self) -> bool:
        """Token launched within last 10 minutes."""
        return self.age_seconds < 600

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


@dataclass
class DEXPairSnapshot:
    """Periodic snapshot for building candle-like history on DEX tokens."""
    address: str
    chain: str
    dex: str
    timestamp: float
    price_usd: float
    volume_usd: float
    liquidity_usd: float
    txns: int


# ==========================================================
# DexScreener client
# ==========================================================

DEXSCREENER_BASE = "https://api.dexscreener.com"


class DexScreenerClient:
    """Free aggregator API covering Pump.fun, Raydium, Jupiter,
    PancakeSwap, Uniswap, and 90+ other DEXes."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ---------------------------------------------------- search
    async def search(self, query: str, limit: int = 50) -> List[DEXToken]:
        """Search tokens by name/symbol. Returns normalized DEXToken list."""
        client = await self._get_client()
        try:
            resp = await client.get(f"{DEXSCREENER_BASE}/latest/dex/search",
                                    params={"q": query})
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"DexScreener search '{query}' failed: {exc}")
            return []
        pairs = data.get("pairs") or data.get("pair") or []
        return self._parse_pairs(pairs[:limit])

    # ---------------------------------------------------- trending
    async def get_trending(self, limit: int = 50) -> List[DEXToken]:
        """Get currently trending tokens across all DEXes.

        Fallback chain:
          1. /token-boosts/top/v1         (top boosted tokens)
          2. /token-boosts/latest/v1      (latest boosted tokens)
          3. /token-profiles/latest/v1    (latest token profiles)
          4. /metas/trending/v1           (trending metas → resolve tokens)
          5. /latest/dex/pairs/solana     (latest Solana pairs by volume)
        """
        client = await self._get_client()
        boosts = []

        # Fallback 1: token-boosts/top/v1
        try:
            resp = await client.get(f"{DEXSCREENER_BASE}/token-boosts/top/v1")
            resp.raise_for_status()
            boosts = resp.json()
            if boosts:
                logger.debug(f"DexScreener trending: {len(boosts)} from token-boosts/top")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"DexScreener trending (boosts top) failed: {exc}")

        # Fallback 2: token-boosts/latest/v1
        if not boosts:
            try:
                resp = await client.get(f"{DEXSCREENER_BASE}/token-boosts/latest/v1")
                resp.raise_for_status()
                boosts = resp.json()
                if boosts:
                    logger.debug(f"DexScreener trending: {len(boosts)} from token-boosts/latest")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"DexScreener trending (boosts latest) failed: {exc}")

        # Fallback 3: token-profiles/latest/v1
        if not boosts:
            try:
                resp = await client.get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
                resp.raise_for_status()
                profiles = resp.json()
                if profiles:
                    # profiles have {chainId, tokenAddress, ...} format
                    boosts = profiles
                    logger.debug(f"DexScreener trending: {len(boosts)} from token-profiles/latest")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"DexScreener trending (profiles) failed: {exc}")

        # Fallback 4: metas/trending/v1 → get trending categories → search tokens
        if not boosts:
            try:
                resp = await client.get(f"{DEXSCREENER_BASE}/metas/trending/v1")
                resp.raise_for_status()
                metas = resp.json()
                if metas and isinstance(metas, list):
                    # Metas are categories (AI, MEME, etc.) — search for tokens in these
                    tokens_from_metas: List[DEXToken] = []
                    for m in metas[:3]:  # Top 3 trending categories
                        slug = m.get("slug", "")
                        if slug:
                            try:
                                # Search tokens by category name
                                search_resp = await client.get(
                                    f"{DEXSCREENER_BASE}/latest/dex/search",
                                    params={"q": slug}
                                )
                                search_resp.raise_for_status()
                                search_data = search_resp.json()
                                pairs = search_data.get("pairs") or []
                                parsed = self._parse_pairs(pairs[:10])
                                tokens_from_metas.extend(parsed)
                            except Exception:
                                pass
                        if len(tokens_from_metas) >= limit:
                            break
                    if tokens_from_metas:
                        # Deduplicate
                        seen_addr: Set[str] = set()
                        unique = []
                        for t in tokens_from_metas:
                            if t.address not in seen_addr:
                                seen_addr.add(t.address)
                                unique.append(t)
                        return unique[:limit]
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"DexScreener trending (metas) failed: {exc}")

        # Fallback 5: search for popular terms to find active tokens
        if not boosts:
            try:
                search_queries = ["pump", "meme", "sol", "pepe"]
                fallback_tokens: List[DEXToken] = []
                for q in search_queries:
                    resp = await client.get(
                        f"{DEXSCREENER_BASE}/latest/dex/search",
                        params={"q": q}
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    pairs = data.get("pairs") or []
                    # Sort by volume
                    pairs.sort(key=lambda p: float((p.get("volume") or {}).get("h24", 0) or 0), reverse=True)
                    parsed = self._parse_pairs(pairs[:10])
                    fallback_tokens.extend(parsed)
                    if len(fallback_tokens) >= limit:
                        break
                if fallback_tokens:
                    seen_addr: Set[str] = set()
                    unique = []
                    for t in fallback_tokens:
                        if t.address not in seen_addr:
                            seen_addr.add(t.address)
                            unique.append(t)
                    return unique[:limit]
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"DexScreener trending (search fallback) failed: {exc}")

        if not boosts:
            return []

        # Resolve boost entries to full token data
        tokens = []
        seen: Set[str] = set()
        for b in boosts[:limit * 2]:
            addr = b.get("tokenAddress", "")
            chain = b.get("chainId", "")
            key = f"{chain}:{addr}"
            if key in seen or not addr:
                continue
            seen.add(key)
            t = await self.get_token(addr, chain)
            if t:
                tokens.append(t)
            if len(tokens) >= limit:
                break
        return tokens

    # ---------------------------------------------------- token info
    async def get_token(self, address: str, chain: str = "") -> Optional[DEXToken]:
        """Fetch full info for a single token by address."""
        client = await self._get_client()
        try:
            if chain:
                # Use the new v1 endpoint
                url = f"{DEXSCREENER_BASE}/token-pairs/v1/{chain}/{address}"
            else:
                url = f"{DEXSCREENER_BASE}/tokens/v1/{address}"
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"DexScreener token {chain}:{address} failed: {exc}")
            return None
        # v1 endpoint returns array directly, not {pairs: [...]}
        pairs = data if isinstance(data, list) else (data.get("pairs") or data.get("pair") or [])
        if not pairs:
            return None
        tokens = self._parse_pairs(pairs)
        return tokens[0] if tokens else None

    # ---------------------------------------------------- latest pairs on chain
    async def get_latest_pairs(self, chain: str = "solana", limit: int = 50) -> List[DEXToken]:
        """Get recently created pools on a given chain — for new-listing sniper.
        
        Strategy: Use search with popular chain-specific queries to find new tokens,
        then sort by creation time.
        """
        client = await self._get_client()
        all_tokens: List[DEXToken] = []

        # Search with chain-specific queries to discover new tokens
        queries = ["pump", "new", "sol", "meme"] if chain == "solana" else ["new", "token"]
        for query in queries:
            try:
                resp = await client.get(
                    f"{DEXSCREENER_BASE}/latest/dex/search",
                    params={"q": query}
                )
                resp.raise_for_status()
                data = resp.json()
                pairs = data.get("pairs") or []
                # Filter by chain
                chain_pairs = [p for p in pairs if p.get("chainId", "") == chain]
                tokens = self._parse_pairs(chain_pairs[:limit])
                for t in tokens:
                    if not any(existing.address == t.address for existing in all_tokens):
                        all_tokens.append(t)
                if len(all_tokens) >= limit:
                    break
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"DexScreener latest pairs search '{query}' {chain}: {exc}")

        # Sort by creation time (newest first)
        all_tokens.sort(key=lambda t: t.created_at, reverse=True)
        return tokens

    # ---------------------------------------------------- token history (OHLCV)
    async def get_token_candles(
        self, chain: str, pair_address: str, timeframe: str = "1m", count: int = 200
    ) -> List[Candle]:
        """Fetch OHLCV candles for a DEX pair from DexScreener."""
        client = await self._get_client()
        # DexScreener candle endpoint:
        # GET /dex/tokens/{address} doesn't give candles, but the chart endpoint does.
        # We fall back to GeckoTerminal which has a clean OHLCV API.
        try:
            return await self._geckoterminal_candles(chain, pair_address, timeframe, count)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"candles for {pair_address}: {exc}")
            return []

    async def _geckoterminal_candles(
        self, chain: str, pool_address: str, timeframe: str, count: int
    ) -> List[Candle]:
        """GeckoTerminal OHLCV endpoint (free, no key).

        GET https://api.geckoterminal.com/api/v2/networks/{chain}/pools/{pool}/ohlcv/{tf}
        """
        client = await self._get_client()
        # map chain names to GeckoTerminal network IDs
        chain_map = {
            "solana": "solana", "bsc": "bsc", "ethereum": "eth",
            "base": "base", "arbitrum": "arbitrum",
        }
        net = chain_map.get(chain, chain)
        # map our TFs to GeckoTerminal TFs
        tf_map = {"1m": "minute", "5m": "minute", "15m": "minute",
                  "1h": "hour", "4h": "hour", "1d": "day"}
        gt_tf = tf_map.get(timeframe, "minute")
        # GeckoTerminal only supports minute/hour/day; we aggregate client-side for 5m/15m
        url = f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pool_address}/ohlcv/{gt_tf}"
        params = {"limit": min(count, 1000), "aggregate": 1}
        if timeframe == "5m":
            params["aggregate"] = 5
        elif timeframe == "15m":
            params["aggregate"] = 15
        elif timeframe == "1h":
            params["aggregate"] = 1
        elif timeframe == "4h":
            params["aggregate"] = 4
        resp = await client.get(url, params=params,
                                headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        candles: List[Candle] = []
        for item in items:
            # GeckoTerminal: [timestamp, open, high, low, close, volume]
            ts, o, h, l, c, v = item
            candles.append(Candle(
                timestamp=int(ts),
                open=float(o), high=float(h), low=float(l),
                close=float(c), volume=float(v),
            ))
        return candles

    # ---------------------------------------------------- parser
    def _parse_pairs(self, pairs: List[dict]) -> List[DEXToken]:
        tokens: List[DEXToken] = []
        for p in pairs:
            try:
                base = p.get("baseToken", {})
                quote = p.get("quoteToken", {})
                price_usd = float(p.get("priceUsd") or 0)
                price_native = float(p.get("priceNative") or 0)
                liquidity = float((p.get("liquidity") or {}).get("usd") or 0)
                vol = p.get("volume") or {}
                txns = p.get("txns") or {}
                total_txns = 0
                for period in txns.values():
                    buys = (period or {}).get("buys", 0)
                    sells = (period or {}).get("sells", 0)
                    total_txns += buys + sells
                price_change = p.get("priceChange") or {}
                created_at = 0.0
                # DexScreener doesn't always give pairCreatedAt; use info if present
                info = p.get("info") or {}
                pair_created = p.get("pairCreatedAt") or info.get("createdAt")
                if pair_created:
                    try:
                        created_at = float(pair_created) / 1000.0
                    except (TypeError, ValueError):
                        pass
                age = time.time() - created_at if created_at else 0.0
                chain_id = p.get("chainId", "")
                dex_id = p.get("dexId", "")
                token = DEXToken(
                    address=base.get("address", ""),
                    chain=chain_id,
                    dex=dex_id,
                    symbol=base.get("symbol", ""),
                    name=base.get("name", ""),
                    pair_address=p.get("pairAddress", ""),
                    price_usd=price_usd,
                    price_native=price_native,
                    liquidity_usd=liquidity,
                    volume_24h_usd=float(vol.get("h24") or 0),
                    volume_6h_usd=float(vol.get("h6") or 0),
                    volume_1h_usd=float(vol.get("h1") or 0),
                    txns_24h=total_txns,
                    mcap=float(p.get("marketCaps", {}).get("fdv", 0) or p.get("fdv", 0) or 0),
                    fdv=float(p.get("fdv") or 0),
                    price_change_24h_pct=float(price_change.get("h24") or 0),
                    price_change_1h_pct=float(price_change.get("h1") or 0),
                    price_change_5m_pct=float(price_change.get("m5") or 0),
                    created_at=created_at,
                    age_seconds=age,
                    socials=p.get("links", []) if isinstance(p.get("links"), list) else [],
                    url=p.get("url", ""),
                )
                tokens.append(token)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"DexScreener parse error: {exc}")
        return tokens


# ==========================================================
# Pump.fun client (Solana launchpad)
# ==========================================================

PUMPFUN_API = "https://pumpportal.fun/api"


class PumpFunClient:
    """Pump.fun — گراند-صفر میم‌کوین‌ها روی سولانا.

    توکن‌هایی که بعداً ۱۰۰ برابر می‌شوند اینجا متولد می‌شوند.
    از چند منبع داده استفاده می‌کنیم:
      1. Pump.fun API — لیست توکن‌های تازه
      2. DexScreener batch lookup — قیمت/نقدینگی/حجم واقعی
      3. PumpPortal API — داده‌های bonding curve
    """

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def get_new_tokens(self, limit: int = 50) -> List[DEXToken]:
        """Fetch recently launched tokens from Pump.fun + enrich with DexScreener.

        Multi-source approach:
          1. Get token list from Pump.fun API (addresses, names, timestamps)
          2. Batch-enrich with DexScreener for real price/volume/liquidity
          3. Tokens not yet on DEX get bonding-curve data from PumpPortal
        """
        # Step 1: Get token list from Pump.fun
        raw_tokens = await self._fetch_pumpfun_list(limit)
        if not raw_tokens:
            # Fallback: search DexScreener for fresh Solana pump tokens
            return await self._fallback_dexscreener(limit)

        # Step 2: Enrich with DexScreener real data
        enriched = await self._enrich_with_dexscreener(raw_tokens)

        # Step 3: For tokens not yet enriched, try PumpPortal bonding curve
        for i, t in enumerate(enriched):
            if t.price_usd <= 0 and t.address:
                bonding = await self._fetch_bonding_curve(t.address)
                if bonding:
                    enriched[i] = bonding

        return enriched[:limit]

    async def _fetch_pumpfun_list(self, limit: int) -> List[dict]:
        """Try multiple Pump.fun API endpoints."""
        client = await self._get_client()
        endpoints = [
            # Primary: Pump.fun frontend API
            ("https://frontend-api-v3.pump.fun/coins", {
                "limit": limit, "sort": "created_timestamp",
                "order": "DESC", "offset": 0,
            }),
            # Alt: pumpportal
            ("https://pumpportal.fun/api/data/coins", {
                "limit": limit, "sort": "created_timestamp",
                "order": "DESC", "offset": 0,
            }),
            # Alt: newer endpoint
            ("https://client-api.pump.fun/coins", {
                "limit": limit, "sort": "created_timestamp",
                "order": "DESC", "offset": 0,
            }),
        ]
        for url, params in endpoints:
            try:
                resp = await client.get(url, params=params,
                                        headers={"Accept": "application/json"},
                                        timeout=10.0)
                resp.raise_for_status()
                data = resp.json()
                coins = data if isinstance(data, list) else data.get("data", data.get("coins", []))
                if coins and len(coins) > 0:
                    logger.debug(f"PumpFun API: {len(coins)} tokens from {url}")
                    return coins
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"PumpFun endpoint {url} failed: {exc}")
                continue
        return []

    async def _enrich_with_dexscreener(self, raw_tokens: List[dict]) -> List[DEXToken]:
        """Given raw Pump.fun token data, batch-enrich with DexScreener
        for real market data (price, volume, liquidity, txns)."""
        client = await self._get_client()
        # Collect all mint addresses
        mints = []
        mint_map = {}  # mint -> raw token data
        for c in raw_tokens:
            mint = c.get("mint", c.get("address", ""))
            if mint:
                mints.append(mint)
                mint_map[mint] = c

        if not mints:
            return []

        # DexScreener batch: up to 30 tokens per call
        enriched_map: Dict[str, DEXToken] = {}
        for i in range(0, len(mints), 30):
            batch = mints[i:i + 30]
            try:
                resp = await client.get(
                    f"{DEXSCREENER_BASE}/tokens/v1/solana/{','.join(batch)}",
                    timeout=10.0,
                )
                resp.raise_for_status()
                pairs = resp.json()
                parsed = DexScreenerClient()._parse_pairs(pairs)
                for t in parsed:
                    enriched_map[t.address] = t
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"DexScreener batch enrich failed: {exc}")

        # Merge: use DexScreener data where available, fallback to Pump.fun raw
        tokens: List[DEXToken] = []
        now = time.time()
        for mint, raw in mint_map.items():
            if mint in enriched_map:
                t = enriched_map[mint]
                # Override created_at from Pump.fun if DexScreener doesn't have it
                if t.created_at == 0:
                    created = float(raw.get("created_timestamp", 0))
                    if created > 1e12:
                        created /= 1000.0
                    t.created_at = created
                    t.age_seconds = now - created if created else 0
                t.dex = "pumpfun" if t.dex in ("pumpfun", "raydium-clmm", "") else t.dex
                if not t.url:
                    t.url = f"https://pump.fun/{mint}"
                tokens.append(t)
            else:
                # Token not on DEX yet — bonding curve only
                created = float(raw.get("created_timestamp", 0))
                if created > 1e12:
                    created /= 1000.0
                age = now - created if created else 0
                # Try to get market cap from Pump.fun data
                mcap = 0.0
                for key in ("usd_marketcap", "market_cap", "usdMarketCap", "marketCap"):
                    val = raw.get(key, 0)
                    if val:
                        mcap = float(val)
                        break
                tokens.append(DEXToken(
                    address=mint,
                    chain="solana",
                    dex="pumpfun",
                    symbol=raw.get("symbol", ""),
                    name=raw.get("name", ""),
                    pair_address=raw.get("pair_address", ""),
                    price_usd=mcap / 1_000_000_000 if mcap else 0,  # assume 1B supply
                    price_native=float(raw.get("sol_price", 0) or 0),
                    liquidity_usd=mcap * 0.8 if mcap else 0,  # bonding curve ~80% locked
                    volume_24h_usd=float(raw.get("volume_24h", 0) or 0),
                    volume_1h_usd=float(raw.get("volume_1h", 0) or 0),
                    txns_24h=int(raw.get("txns_24h", 0) or 0),
                    mcap=mcap,
                    fdv=mcap,
                    price_change_24h_pct=float(raw.get("change_24h", 0) or 0),
                    price_change_1h_pct=float(raw.get("change_1h", 0) or 0),
                    created_at=created,
                    age_seconds=age,
                    url=f"https://pump.fun/{mint}",
                ))
        return tokens

    async def _fetch_bonding_curve(self, mint: str) -> Optional[DEXToken]:
        """Fetch bonding-curve price from PumpPortal for tokens not yet on DEX."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"https://pumpportal.fun/api/data/token/{mint}",
                timeout=8.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None
            # PumpPortal returns bonding curve info
            sol_balance = float(data.get("solInBondingCurve", 0) or 0)
            token_balance = float(data.get("tokenInBondingCurve", 0) or 0)
            complete = data.get("complete", False)
            # Calculate price from bonding curve
            if token_balance > 0 and sol_balance > 0:
                # SOL price ~ $150 (will be corrected by DexScreener later)
                price_sol = sol_balance / token_balance
                price_usd = price_sol * 150  # rough SOL price
            else:
                price_usd = 0
            created = float(data.get("createdTimestamp", 0) or 0)
            if created > 1e12:
                created /= 1000.0
            return DEXToken(
                address=mint,
                chain="solana",
                dex="pumpfun",
                symbol=data.get("symbol", ""),
                name=data.get("name", ""),
                price_usd=price_usd,
                liquidity_usd=sol_balance * 150 if sol_balance else 0,
                mcap=price_usd * 1_000_000_000,
                created_at=created,
                age_seconds=time.time() - created if created else 0,
                url=f"https://pump.fun/{mint}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PumpPortal bonding curve {mint}: {exc}")
            return None

    async def _fallback_dexscreener(self, limit: int) -> List[DEXToken]:
        """Fallback: search DexScreener for fresh Solana tokens."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{DEXSCREENER_BASE}/latest/dex/search",
                params={"q": "pump solana"},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            pairs = data.get("pairs") or []
            return DexScreenerClient()._parse_pairs(pairs[:limit])
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PumpFun fallback DexScreener: {exc}")
            return []


# ==========================================================
# GMGN.ai — Smart Money & Wallet Analytics
# ==========================================================
# GMGN tracks smart money wallets, bundle detection, insider activity.
# Free API (no key needed for basic endpoints).

GMGN_BASE = "https://gmgn.ai/defi/quotation/v1"


class GMGNClient:
    """GMGN.ai — ردیابی پول هوشمند و تحلیل کیف پول.

    اطلاعاتی که فراهم می‌کند:
    - آدرس کیف پول‌های هوشمند (Smart Money)
    - تشخیص Bundle (خرید همزمان از چند کیف پول)
    - فعالیت Insider (کیف پول توسعه‌دهنده)
    - Top traders روی هر توکن
    """

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def get_new_pairs(self, chain: str = "solana", limit: int = 50) -> List[dict]:
        """Get newly created token pairs with smart money data."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{GMGN_BASE}/swaps/pair/{chain}/new_pairs",
                params={"limit": limit, "orderby": "creation_time", "direction": "desc"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("pairs", []) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"GMGN new pairs failed: {exc}")
            return []

    async def get_token_security(self, chain: str, address: str) -> dict:
        """Get token security info (top holders, dev holdings, etc)."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{GMGN_BASE}/tokens/token_security/{chain}",
                params={"address": address},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"GMGN security {chain}:{address}: {exc}")
            return {}

    async def get_smart_money_tokens(self, chain: str = "solana", limit: int = 30) -> List[dict]:
        """Get tokens that smart money wallets are buying right now."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{GMGN_BASE}/wallet/smart_money/{chain}/top",
                params={"limit": limit, "period": "1h"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("tokens", []) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"GMGN smart money failed: {exc}")
            return []

    async def get_top_holders(self, chain: str, address: str, limit: int = 20) -> List[dict]:
        """Get top holders of a token — used for dev holdings check."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{GMGN_BASE}/tokens/top_holders/{chain}",
                params={"address": address, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("holders", []) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"GMGN top holders {chain}:{address}: {exc}")
            return []


# ==========================================================
# Birdeye — Token Analytics & Price Data
# ==========================================================
# Birdeye provides detailed price/volume analytics for Solana tokens.
# Free tier available (with rate limits).

BIRDEYE_BASE = "https://public-api.birdeye.so"


class BirdeyeClient:
    """Birdeye — تحلیل قیمت و حجم توکن‌های سولانا.

    اطلاعاتی که فراهم می‌کند:
    - قیمت لحظه‌ای دقیق
    - حجم معاملات ۲۴ ساعت
    - تعداد خریداران/فروشندگان
    - قیمت OHLCV
    """

    def __init__(self, api_key: str = "", timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"User-Agent": "Mozilla/5.0", "x-chain": "solana"}
            if self.api_key:
                headers["X-API-KEY"] = self.api_key
            self._client = httpx.AsyncClient(
                timeout=self.timeout, headers=headers, follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def get_token_overview(self, address: str) -> dict:
        """Get token price overview (price, volume, liquidity, holders)."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{BIRDEYE_BASE}/defi/token_overview",
                params={"address": address},
            )
            resp.raise_for_status()
            return resp.json().get("data", {}) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Birdeye overview {address}: {exc}")
            return {}

    async def get_token_traders(self, address: str, limit: int = 20) -> dict:
        """Get buy/sell counts and unique traders."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{BIRDEYE_BASE}/defi/token_trades",
                params={"address": address, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json().get("data", {}) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Birdeye traders {address}: {exc}")
            return {}


# ==========================================================
# Unified DEX manager
# ==========================================================

class DEXManager:
    """Coordinates all DEX data sources and provides a unified interface
    that mirrors the CEX ExchangeManager so the rest of the app can use
    DEX tokens interchangeably.

    Data sources:
      - DexScreener  — price/volume/liquidity across 90+ DEXes
      - Pump.fun     — freshest Solana meme coin launches
      - GMGN.ai      — smart money tracking, bundle detection
      - Birdeye      — detailed Solana token analytics
    """

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.enabled_dexes: Set[str] = set(self.config.get("enabled_dexes",
            ["pumpfun", "raydium", "pancakeswap", "uniswap"]))
        self.enabled_chains: Set[str] = set(self.config.get("enabled_chains",
            ["solana", "bsc", "ethereum"]))
        self.min_liquidity_usd = float(self.config.get("min_liquidity_usd", 10_000))
        self.min_volume_1h_usd = float(self.config.get("min_volume_1h_usd", 1_000))
        self.max_age_hours = float(self.config.get("max_age_hours", 24))
        self.ds = DexScreenerClient()
        self.pf = PumpFunClient()
        self.gmgn = GMGNClient()
        self.birdeye = BirdeyeClient()

    async def close(self) -> None:
        await self.ds.close()
        await self.pf.close()
        await self.gmgn.close()
        await self.birdeye.close()

    # ---------------------------------------------------- universe discovery
    async def discover_tokens(self, limit: int = 100) -> List[DEXToken]:
        """Discover fresh meme coins across all enabled DEXes.

        Strategy:
          1. Pump.fun newest launches (highest alpha)
          2. DexScreener latest pairs on each enabled chain
          3. DexScreener trending
        """
        tasks: List = []

        if "pumpfun" in self.enabled_dexes:
            tasks.append(self._safe(self.pf.get_new_tokens(limit=limit), "pumpfun_new"))

        for chain in self.enabled_chains:
            tasks.append(self._safe(self.ds.get_latest_pairs(chain, limit), f"latest_{chain}"))

        tasks.append(self._safe(self.ds.get_trending(limit), "trending"))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_tokens: List[DEXToken] = []
        seen: Set[str] = set()
        for r in results:
            if isinstance(r, Exception) or not r:
                continue
            for t in r:
                key = f"{t.chain}:{t.address}"
                if key in seen or not t.address:
                    continue
                seen.add(key)
                all_tokens.append(t)

        # Filter by liquidity / volume / age
        filtered = [
            t for t in all_tokens
            if self._passes_filter(t)
        ]
        # Sort: newest first (freshness = alpha)
        filtered.sort(key=lambda t: t.created_at, reverse=True)
        logger.info(f"DEX discovery: {len(all_tokens)} found, {len(filtered)} passed filters")
        return filtered[:limit]

    def _passes_filter(self, t: DEXToken) -> bool:
        # Pump.fun tokens with very low liquidity are allowed if brand-new
        if t.is_brand_new and t.dex == "pumpfun":
            return True
        if t.liquidity_usd < self.min_liquidity_usd:
            return False
        if t.volume_1h_usd < self.min_volume_1h_usd and t.age_seconds > 3600:
            # old + low volume = dead, skip
            return False
        if t.age_seconds > self.max_age_hours * 3600:
            return False
        if t.dex not in self.enabled_dexes and t.dex:
            return False
        return True

    async def _safe(self, coro, label: str):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"DEX source {label} failed: {exc}")
            return []

    # ---------------------------------------------------- token detail
    async def get_token(self, address: str, chain: str = "solana") -> Optional[DEXToken]:
        return await self.ds.get_token(address, chain)

    async def get_token_candles(
        self, chain: str, pair_address: str, timeframe: str = "1m", count: int = 200
    ) -> List[Candle]:
        return await self.ds.get_token_candles(chain, pair_address, timeframe, count)

    # ---------------------------------------------------- live price polling
    async def poll_prices(self, tokens: List[DEXToken]) -> Dict[str, DEXToken]:
        """Refresh prices for a watchlist of tokens. Returns {address: updated_token}."""
        out: Dict[str, DEXToken] = {}
        # DexScreener allows batch: GET /tokens/v1/{chain}/{addr1,addr2,...}
        # Group by chain for batch efficiency
        by_chain: Dict[str, List[str]] = {}
        for t in tokens:
            by_chain.setdefault(t.chain, []).append(t.address)
        client = await self.ds._get_client()
        for chain, addrs in by_chain.items():
            # batch up to 30 addresses per call (DexScreener limit)
            for i in range(0, len(addrs), 30):
                batch = addrs[i:i + 30]
                try:
                    resp = await client.get(
                        f"{DEXSCREENER_BASE}/tokens/v1/{chain}/{','.join(batch)}"
                    )
                    resp.raise_for_status()
                    pairs = resp.json()
                    parsed = self.ds._parse_pairs(pairs)
                    for t in parsed:
                        out[f"{t.chain}:{t.address}"] = t
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"DEX poll batch {chain}: {exc}")
        return out


# singleton --------------------------------------------------
_dex_mgr: Optional[DEXManager] = None


def get_dex_manager(config: Dict[str, Any] | None = None) -> DEXManager:
    global _dex_mgr
    if _dex_mgr is None:
        _dex_mgr = DEXManager(config)
    return _dex_mgr
